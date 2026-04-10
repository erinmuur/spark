"""
Spark Slack bot — watches a channel for video links and saves them to the DB.
Run separately from the Flask app: python bot.py
"""
import os
import threading

from dotenv import load_dotenv
load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app import app as flask_app
from models import db, Video
from ingest import extract_video_urls, detect_platform, fetch_metadata
import ai

slack_app = App(token=os.environ['SLACK_BOT_TOKEN'])

WATCHED_CHANNEL = os.environ.get('SLACK_CHANNEL_ID', '')


def _resolve_slack_user(client, user_id):
    """Return display name for a Slack user ID, falling back to the raw ID."""
    try:
        info = client.users_info(user=user_id)
        profile = info.get('user', {}).get('profile', {})
        return profile.get('display_name') or profile.get('real_name') or user_id
    except Exception:
        return user_id


def process_video(url, channel, ts, user, text, client):
    """Fetch metadata, classify, and reply in thread. Runs in a background thread."""
    import re
    with flask_app.app_context():
        existing = Video.query.filter_by(url=url).first()
        if existing:
            client.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text='Already in Spark ✓'
            )
            return

        # Resolve user display name and extract any notes (message text minus URLs)
        display_name = _resolve_slack_user(client, user)
        notes = re.sub(r'https?://\S+', '', text).strip()

        # Save immediately so duplicates from other messages are caught
        video = Video(
            url=url,
            platform=detect_platform(url),
            slack_user=display_name,
            slack_channel=channel,
            slack_ts=ts,
            slack_message=notes or None,
        )
        db.session.add(video)
        db.session.commit()
        video_id = video.id

        # Fetch metadata
        meta = fetch_metadata(url)
        if meta and 'error' not in meta:
            video.creator = meta.get('creator', '')
            video.title = meta.get('title', '')
            video.caption = meta.get('caption', '')
            video.thumbnail_url = meta.get('thumbnail_url', '')
            video.raw_metadata = meta.get('raw', '')
            db.session.commit()

        # Classify against frameworks
        from models import Framework
        frameworks = Framework.query.all()
        framework_id, analysis = ai.classify_video(video, frameworks)
        if framework_id:
            video.framework_id = framework_id
        if analysis:
            video.analysis = analysis
        db.session.commit()

        # Post thread confirmation
        framework_name = video.framework.name if video.framework else 'Unclassified'
        creator_str = f' by {video.creator}' if video.creator else ''
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f'Saved to Spark ✓\n*Framework:* {framework_name}{creator_str}'
        )


@slack_app.event('message')
def handle_message(body, client):
    event = body.get('event', {})

    # Ignore bot messages and edits
    if event.get('bot_id') or event.get('subtype') in ('bot_message', 'message_changed', 'message_deleted'):
        return

    # If a channel filter is set, only process that channel
    channel = event.get('channel', '')
    if WATCHED_CHANNEL and channel != WATCHED_CHANNEL:
        return

    text = event.get('text', '')
    urls = extract_video_urls(text)
    if not urls:
        return

    ts = event.get('ts', '')
    user = event.get('user', '')

    for url in urls:
        t = threading.Thread(
            target=process_video,
            args=(url, channel, ts, user, text, client),
            daemon=True
        )
        t.start()


if __name__ == '__main__':
    with flask_app.app_context():
        db.create_all()

    print('Starting Spark bot...')
    handler = SocketModeHandler(slack_app, os.environ['SLACK_APP_TOKEN'])
    handler.start()
