"""Scheduled metrics refresh for campaign UGC.

Views keep climbing for weeks after a video goes up, so the single scrape at
ingest under-reports. Every campaign video is re-scraped daily for its first
14 days in Spark, then weekly.

Runs as its own process (Procfile `refresher`) so each video is scraped once
per interval — the web app runs several gunicorn workers, and a timer in each
would multiply the (partly paid) scrapes.

    python refresh.py
"""
import sys
import time
from datetime import datetime, timedelta

from app import app, _apply_video_metrics
from models import db, Video, CampaignVideo
from ingest import fetch_metadata

DAILY_WINDOW = timedelta(days=14)
# A little under the nominal interval, so an hourly tick can't slip a whole day
DAILY_INTERVAL = timedelta(hours=23)
WEEKLY_INTERVAL = timedelta(days=7) - timedelta(hours=1)
# After a failed scrape, wait this long rather than retrying (and paying) every tick
RETRY_AFTER = timedelta(hours=6)
TICK_SECONDS = 3600
PAUSE_BETWEEN_SCRAPES = 5  # seconds — stay polite to the platforms


def refresh_interval(video, now):
    age = now - (video.created_at or now)
    return DAILY_INTERVAL if age <= DAILY_WINDOW else WEEKLY_INTERVAL


def due_video_ids(now, last_attempt):
    """Campaign/UGC videos whose metrics are older than their interval."""
    in_campaign = db.session.query(CampaignVideo.video_id)
    tracked = Video.query.filter(
        db.or_(Video.post_id.isnot(None), Video.id.in_(in_campaign))
    ).all()
    due = []
    for v in tracked:
        if v.metrics_updated_at and now - v.metrics_updated_at < refresh_interval(v, now):
            continue
        attempted = last_attempt.get(v.id)
        if attempted and now - attempted < RETRY_AFTER:
            continue
        due.append(v.id)
    return due


def run_pass(last_attempt=None):
    """Refresh every due video once. Returns counts for logging."""
    last_attempt = {} if last_attempt is None else last_attempt
    with app.app_context():
        due = due_video_ids(datetime.utcnow(), last_attempt)

    refreshed = 0
    for i, video_id in enumerate(due):
        if i:
            time.sleep(PAUSE_BETWEEN_SCRAPES)
        last_attempt[video_id] = datetime.utcnow()
        with app.app_context():
            video = Video.query.get(video_id)
            if not video:
                continue
            try:
                meta = fetch_metadata(video.url, for_refresh=True)
            except Exception as e:
                print(f'[refresh] video {video_id} failed: {e}', file=sys.stderr, flush=True)
                continue
            if meta and 'error' not in meta:
                _apply_video_metrics(video_id, meta)
                refreshed += 1
            else:
                print(f'[refresh] video {video_id} got no data: {(meta or {}).get("error")}',
                      file=sys.stderr, flush=True)

    if due:
        print(f'[refresh] refreshed {refreshed}/{len(due)} due videos', flush=True)
    return {'due': len(due), 'refreshed': refreshed}


if __name__ == '__main__':
    print('[refresh] metrics refresher started', flush=True)
    attempts = {}
    while True:
        try:
            run_pass(attempts)
        except Exception as e:
            print(f'[refresh] pass crashed: {e}', file=sys.stderr, flush=True)
        time.sleep(TICK_SECONDS)
