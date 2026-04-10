"""
Modal-based TRIBE v2 inference for Spark.

Runs on a T4 GPU in Modal. Accepts a video URL, downloads it, runs
TribeModel inference, extracts frames at peak/valley timestamps, and
returns scores + frames for Claude Vision suggestions.
"""
import modal
import os

app = modal.App("spark-tribe")

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
        "git+https://github.com/facebookresearch/tribev2.git@72399081ed3f1040c4d996cefb2864a4c46f5b8e",
        "neuraltrain==0.0.2",
        "whisperx",
        "yt-dlp",
        "huggingface_hub",
        "certifi",
        "numpy",
        "Pillow",
    )
)

# Cache the model weights in a Modal volume so we only download once
model_volume = modal.Volume.from_name("spark-tribe-models", create_if_missing=True)


@app.function(
    image=image,
    gpu="T4",
    timeout=300,
    secrets=[modal.Secret.from_name("huggingface")],
    volumes={"/model-cache": model_volume},
)
@modal.fastapi_endpoint(method="POST")
def run_tribe_inference(item: dict) -> dict:
    video_url: str = item["url"]
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

    # Auth
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        huggingface_hub.login(token=hf_token, add_to_git_credential=False)

    cache_dir = "/model-cache/tribev2"
    os.makedirs(cache_dir, exist_ok=True)

    # Patch config to use CUDA (GPU is available on Modal)
    _patch_config_for_gpu(cache_dir)

    # Load model (cached in volume after first run)
    model = TribeModel.from_pretrained("facebook/tribev2", cache_folder=cache_dir)

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


def _patch_config_for_gpu(cache_dir: str):
    """Ensure TRIBE config says device: cuda (correct for Modal GPU)."""
    import glob as _glob
    import re

    # Check both the custom cache dir and the default HF cache
    patterns = [
        os.path.join(cache_dir, "**", "config.yaml"),
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
