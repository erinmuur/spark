"""TRIBE v2 in-app inference module.

Called from a background thread by app.py. Downloads the video via yt-dlp,
runs TribeModel inference on CPU (MPS on macOS 14+), saves per-second mean
brain activation scores to the Video row, then calls Claude for suggestions.
"""
import json
import os
import tempfile
import subprocess
import glob
import logging

logger = logging.getLogger(__name__)

_model = None  # loaded lazily and cached for the process lifetime
_MODEL_ID = 'facebook/tribev2'
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'tribe_cache')


def _load_model():
    global _model
    if _model is None:
        from tribev2.demo_utils import TribeModel
        import huggingface_hub
        hf_token = os.environ.get('HF_TOKEN')
        if hf_token:
            huggingface_hub.login(token=hf_token, add_to_git_credential=False)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        logger.info('Loading TRIBE v2 model (first run downloads ~1 GB)…')
        _model = TribeModel.from_pretrained(_MODEL_ID, cache_folder=_CACHE_DIR)
        logger.info('TRIBE v2 model loaded.')
    return _model


def _download_video(url, tmpdir):
    """Download video to tmpdir using yt-dlp. Returns path to file."""
    out_template = os.path.join(tmpdir, 'video.%(ext)s')
    result = subprocess.run(
        ['yt-dlp', '-o', out_template, '-f', 'mp4/best[ext=mp4]/best',
         '--no-playlist', '--quiet', url],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f'yt-dlp failed: {result.stderr.strip()}')
    files = glob.glob(os.path.join(tmpdir, 'video.*'))
    if not files:
        raise RuntimeError('yt-dlp produced no output file')
    return files[0]


def run_inference(video_id):
    """
    Full pipeline: download → infer → save scores → generate suggestions.
    Designed to run in a background thread with its own Flask app context.
    Must be called inside `with app.app_context()`.
    """
    # Import here to avoid circular import (tribe.py imported by app.py)
    from models import db, Video
    import ai

    video = Video.query.get(video_id)
    if not video:
        return

    try:
        video.tribe_status = 'running'
        db.session.commit()

        # 1. Load model (cached after first call)
        model = _load_model()

        # 2. Download video to a temp directory
        with tempfile.TemporaryDirectory() as tmpdir:
            logger.info(f'Downloading video {video_id}: {video.url}')
            video_path = _download_video(video.url, tmpdir)
            logger.info(f'Running TRIBE inference on {video_path}')

            # 3. Run inference
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                df = model.get_events_dataframe(video_path=video_path)
                preds, _ = model.predict(events=df)  # (n_seconds, 20484)

        # 4. Mean activation across all cortical vertices per second
        import numpy as np
        scores = preds.mean(axis=1).tolist()
        logger.info(f'TRIBE inference done: {len(scores)}s, peak t={scores.index(max(scores))}s')

        # 5. Save scores
        video.tribe_scores = json.dumps([round(float(v), 6) for v in scores])
        db.session.commit()

        # 6. Generate Claude suggestions
        suggestions = ai.generate_tribe_suggestions(video, scores)
        video.tribe_suggestions = suggestions
        video.tribe_status = 'done'
        db.session.commit()
        logger.info(f'TRIBE analysis complete for video {video_id}')

    except Exception as e:
        logger.error(f'TRIBE inference failed for video {video_id}: {e}')
        try:
            video.tribe_status = f'error: {str(e)[:200]}'
            db.session.commit()
        except Exception:
            pass
