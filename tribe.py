"""TRIBE v2 inference module for Spark.

Delegates GPU inference to Modal (tribe_modal.py). The run_inference()
function is called from a background thread by app.py and handles all
DB writes; the actual compute runs remotely on a Modal T4 GPU.
"""
import json
import os
import logging

logger = logging.getLogger(__name__)


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

        # Run inference on Modal GPU — blocks until complete (~2 min on T4)
        logger.info(f'Dispatching TRIBE inference to Modal for video {video_id}: {video.url}')
        import modal
        run_tribe_inference = modal.Function.from_name("spark-tribe", "run_tribe_inference")
        result = run_tribe_inference.remote(video.url)

        scores = result['scores']
        frames = result['frames']   # {t=Xs: base64_jpeg, ...}
        peak_t = result['peak_t']
        trough_t = result['trough_t']

        logger.info(f'TRIBE inference done: {len(scores)}s, peak={peak_t}s, trough={trough_t}s')

        # Save scores
        video.tribe_scores = json.dumps(scores)
        db.session.commit()

        # Generate Claude Vision suggestions using scores + extracted frames
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
