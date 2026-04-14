"""
Modal-based TRIBE v2 inference for Spark.

Runs on a T4 GPU in Modal. Accepts a video URL, downloads it, runs
TribeModel inference, extracts frames at peak/valley timestamps, and
returns scores + frames for Claude Vision suggestions.
"""
import modal
import os

app = modal.App("spark-tribe")

def _download_model():
    """Pre-download TRIBE v2 weights into the image at build time."""
    import os
    import huggingface_hub
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        huggingface_hub.login(token=hf_token, add_to_git_credential=False)
    from tribev2.demo_utils import TribeModel
    TribeModel.from_pretrained("facebook/tribev2")
    print("TRIBE v2 model downloaded.")


image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "git", "wget")
    .pip_install(
        "torch==2.1.2",
        "torchvision==0.16.2",
        "torchaudio==2.1.2",
        index_url="https://download.pytorch.org/whl/cu118",
    )
    .pip_install(
        "uv",  # provides uvx, required by tribev2's get_events_dataframe for transcription
        "git+https://github.com/facebookresearch/tribev2.git@72399081ed3f1040c4d996cefb2864a4c46f5b8e",
        "neuraltrain==0.0.2",
        "whisperx",
        "yt-dlp",
        "huggingface_hub",
        "certifi",
        "numpy",
        "Pillow",
    )
    .run_commands(
        # Patch tribev2 to skip word-level alignment (WAV2VEC2 embedding step).
        # Word alignment takes ~90 min on CPU. TRIBE only needs segment-level
        # timestamps, so --no_align produces equivalent results much faster.
        "python3 -c \""
        "import re, pathlib; "
        "p = pathlib.Path('/usr/local/lib/python3.11/site-packages/tribev2/eventstransforms.py'); "
        "src = p.read_text(); "
        "patched = src.replace('\\\"--align_model\\\",\\n                \\\"WAV2VEC2_ASR_LARGE_LV60K_960H\\\" if language == \\\"english\\\" else \\\"\\\",', "
        "'\\\"--no_align\\\",'); "
        "p.write_text(patched); "
        "print('Patch applied:', '--no_align' in patched)\""
    )
    .run_function(
        _download_model,
        secrets=[modal.Secret.from_name("huggingface")],
        timeout=600,
    )
)


@app.function(
    image=image,
    gpu="A10G",
    timeout=3600,
    secrets=[modal.Secret.from_name("huggingface")],
)
@modal.asgi_app()
def run_tribe_inference():
    """Raw ASGI endpoint — no FastAPI/pydantic dependency (avoids Modal 1.4.1 conflict)."""
    import json

    async def asgi_app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        try:
            data = json.loads(body)
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _run_inference, data["url"])
            status, response_body = 200, json.dumps(result).encode()
        except Exception as e:
            import traceback
            status = 500
            response_body = json.dumps({"error": str(e), "traceback": traceback.format_exc()}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": response_body})

    return asgi_app


def _run_inference(video_url: str) -> dict:
    """
    Download video, run TRIBE v2 inference, extract peak/valley frames.

    Returns:
        {
            'scores': [float, ...],       # per-second mean cortical activation
            'frames': {                   # base64 JPEG frames at key timestamps
                't=Xs': '<base64>',
                ...
            },
            'peak_t': int,
            'trough_t': int,
        }
    """
    import subprocess
    import tempfile
    import glob
    import base64
    import re
    import warnings
    import numpy as np
    import huggingface_hub
    from tribev2.demo_utils import TribeModel

    # Model is pre-downloaded into the image at build time — no download needed at runtime
    # Patch config to use CUDA
    _patch_config_for_gpu()

    model = TribeModel.from_pretrained("facebook/tribev2")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download video
        out_template = os.path.join(tmpdir, "video.%(ext)s")
        result = subprocess.run(
            ["yt-dlp", "-o", out_template, "-f", "mp4/best[ext=mp4]/best",
             "--no-playlist", "--quiet", video_url],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp failed: {result.stderr.strip()}")

        files = glob.glob(os.path.join(tmpdir, "video.*"))
        if not files:
            raise RuntimeError("yt-dlp produced no output file")
        video_path = files[0]

        # Run TRIBE inference
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = model.get_events_dataframe(video_path=video_path)
            preds, _ = model.predict(events=df)  # (n_seconds, 20484)

        scores = preds.mean(axis=1).tolist()
        peak_t = int(np.argmax(scores))
        trough_t = int(np.argmin(scores))

        # Extract frames at peak, trough, and neighbours for context
        timestamps = _key_timestamps(scores, peak_t, trough_t)
        frames = _extract_frames(video_path, timestamps)

    return {
        "scores": [round(float(v), 6) for v in scores],
        "frames": frames,
        "peak_t": peak_t,
        "trough_t": trough_t,
    }


def _patch_config_for_gpu():
    """Ensure TRIBE config says device: cuda (correct for Modal GPU)."""
    import glob as _glob
    import re

    patterns = [
        os.path.expanduser("~/.cache/huggingface/hub/models--facebook--tribev2/snapshots/*/config.yaml"),
    ]
    for pattern in patterns:
        for config_path in _glob.glob(pattern, recursive=True):
            with open(config_path, "r") as f:
                content = f.read()
            # Make sure it says cuda (not cpu from local dev patches)
            patched = re.sub(r"device:\s*cpu", "device: cuda", content)
            if patched != content:
                with open(config_path, "w") as f:
                    f.write(patched)


def _key_timestamps(scores: list, peak_t: int, trough_t: int) -> list[int]:
    """Return timestamps to extract frames at: peak, trough, +/- 1s neighbours."""
    n = len(scores)
    ts = set()
    for t in [peak_t, trough_t]:
        for offset in [-1, 0, 1]:
            candidate = t + offset
            if 0 <= candidate < n:
                ts.add(candidate)
    return sorted(ts)


def _extract_frames(video_path: str, timestamps: list[int]) -> dict:
    """Extract one JPEG frame per timestamp using ffmpeg. Returns {label: base64}."""
    import subprocess
    import tempfile
    import base64

    frames = {}
    for t in timestamps:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            out_path = f.name
        result = subprocess.run(
            ["ffmpeg", "-ss", str(t), "-i", video_path,
             "-frames:v", "1", "-q:v", "5", "-y", out_path],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0 and os.path.exists(out_path):
            with open(out_path, "rb") as f:
                frames[f"t={t}s"] = base64.b64encode(f.read()).decode()
            os.unlink(out_path)
    return frames
