"""TRIBE v2 inference module for Spark.

Calls the Modal web endpoint (no Modal SDK required in prod).
The run_inference() function is called from a background thread by app.py
and handles all DB writes; compute runs on a Modal T4 GPU.
"""
import json
import os
import logging
import requests

logger = logging.getLogger(__name__)

TRIBE_MODAL_URL = os.environ.get(
    "TRIBE_MODAL_URL",
    "https://readwiseio--spark-tribe-run-tribe-inference.modal.run",
)


def run_inference(video_id):
    """
    Full pipeline: Modal GPU inference → save scores → Claude Vision suggestions.
    Designed to run in a background thread with its own Flask app context.
    Must be called inside `with app.app_context()`.
    """
    from models import db, Video
    import ai

    video = Video.query.get(video_id)
    if not video:
        return

    try:
        video.tribe_status = 'running'
        db.session.commit()

        logger.info(f'Dispatching TRIBE inference to Modal for video {video_id}: {video.url}')

        # Plain HTTP POST — no Modal SDK, no uvx dependency
        response = requests.post(
            TRIBE_MODAL_URL,
            json={"url": video.url},
            timeout=(10, 3660),  # (connect timeout, read timeout) — Modal fn is 3600s max
        )
        if not response.ok:
            raise RuntimeError(f'Modal returned {response.status_code}: {response.text[:500]}')
        result = response.json()

        scores = result['scores']
        frames = result['frames']
        peak_t = result['peak_t']
        trough_t = result['trough_t']

        logger.info(f'TRIBE inference done: {len(scores)}s, peak={peak_t}s, trough={trough_t}s')

        video.tribe_scores = json.dumps(scores)
        db.session.commit()

        suggestions = ai.generate_tribe_suggestions(video, scores, frames=frames)
        video.tribe_suggestions = suggestions
        video.tribe_status = 'done'
        db.session.commit()
        logger.info(f'TRIBE analysis complete for video {video_id}')

    except Exception as e:
        import traceback
        logger.error(f'TRIBE inference failed for video {video_id}: {e}\n{traceback.format_exc()}')
        try:
            video.tribe_status = f'error: {str(e)[:200]}'
            db.session.commit()
        except Exception:
            pass
