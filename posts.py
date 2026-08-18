"""Grouping of the same UGC creative across platforms.

Creators post one video to TikTok, Instagram and sometimes YouTube. Each upload
arrives as its own Video row. This module decides which uploads are the same
creative so they can share a Post page with a per-platform stat breakdown.

Auto-grouping is deliberately conservative: a wrong merge hides a video behind
another creative's page, which is more annoying to undo than merging two posts
by hand. Every decision here is overridable from the post page.
"""

import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from models import db, Video, Post

# A creator's uploads to different platforms can be days apart — but two
# unrelated videos from the same creator are far more likely than a re-post
# after this long.
CROSS_POST_WINDOW = timedelta(days=45)

TRANSCRIPT_THRESHOLD = 0.60
CAPTION_THRESHOLD = 0.55
WEAK_CAPTION_THRESHOLD = 0.30
DURATION_TOLERANCE = 1.5  # seconds


def normalize_handle(creator):
    """Collapse a creator name to a cross-platform key.

    'erin.reads', 'erinreads' and 'Erin Reads' all normalize to 'erinreads',
    which is how the same person's TikTok, Instagram and YouTube identities
    usually differ.
    """
    if not creator:
        return ''
    return re.sub(r'[^a-z0-9]', '', creator.lower().lstrip('@'))


def _normalize_text(text):
    """Strip hashtags, mentions, urls and punctuation for content comparison."""
    if not text:
        return ''
    text = text.lower()
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'[#@]\w+', ' ', text)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _similarity(a, b):
    a, b = _normalize_text(a), _normalize_text(b)
    if not a or not b:
        return 0.0
    # Very short strings produce noisy ratios — require some substance
    if len(a) < 12 or len(b) < 12:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _duration(video):
    if not video.raw_metadata:
        return None
    try:
        raw = json.loads(video.raw_metadata)
    except (json.JSONDecodeError, TypeError):
        return None
    d = raw.get('duration')
    try:
        return float(d) if d is not None else None
    except (TypeError, ValueError):
        return None


def match_score(video, other):
    """How confident we are that two uploads are the same creative (0.0–1.0).

    Returns 0.0 when they're on the same platform — a creator posting the same
    video twice to TikTok is a separate post, not a cross-post.
    """
    if video.id == other.id:
        return 0.0
    if video.platform and other.platform and video.platform == other.platform:
        return 0.0

    transcript_sim = _similarity(video.transcript, other.transcript)
    caption_sim = max(
        _similarity(video.caption, other.caption),
        _similarity(video.title, other.title),
        _similarity(video.caption, other.title),
        _similarity(video.title, other.caption),
    )

    d1, d2 = _duration(video), _duration(other)
    duration_match = (
        d1 is not None and d2 is not None
        and d1 > 0 and abs(d1 - d2) <= DURATION_TOLERANCE
    )

    if transcript_sim >= TRANSCRIPT_THRESHOLD:
        return max(transcript_sim, 0.8 if duration_match else transcript_sim)
    if caption_sim >= CAPTION_THRESHOLD:
        return caption_sim
    if duration_match and caption_sim >= WEAK_CAPTION_THRESHOLD:
        return 0.6
    return 0.0


def find_matching_post(video, min_score=0.55):
    """Find an existing Post whose videos look like the same creative.

    Only considers posts by the same normalized creator handle, within the
    cross-post window, that don't already have an upload on this platform.
    """
    key = normalize_handle(video.creator)
    if not key:
        return None

    cutoff = (video.created_at or datetime.utcnow()) - CROSS_POST_WINDOW
    candidates = Post.query.filter(
        Post.creator_key == key,
        Post.id != (video.post_id or -1),
    ).all()

    best, best_score = None, 0.0
    for post in candidates:
        if post.created_at and post.created_at < cutoff:
            continue
        if video.platform and video.platform in post.platforms:
            continue
        scores = [match_score(video, other) for other in post.videos]
        score = max(scores) if scores else 0.0
        if score > best_score:
            best, best_score = post, score

    return best if best_score >= min_score else None


def attach_to_post(video, post):
    """Move a video onto a post, keeping the post's denormalized fields fresh."""
    video.post_id = post.id
    if not post.creator and video.creator:
        post.creator = video.creator
        post.creator_key = normalize_handle(video.creator)
    if not post.title and video.title:
        post.title = video.title
    return post


def create_post_for(video, campaign_id=None):
    post = Post(
        title=video.title or None,
        creator=video.creator or None,
        creator_key=normalize_handle(video.creator),
        campaign_id=campaign_id,
        created_at=video.created_at or datetime.utcnow(),
    )
    db.session.add(post)
    db.session.flush()
    video.post_id = post.id
    return post


def group_video(video, campaign_id=None, allow_create=True):
    """Place a video into the right Post, creating one if nothing matches.

    Returns the Post, or None when the video was left ungrouped.
    """
    if video.post_id:
        return Post.query.get(video.post_id)

    match = find_matching_post(video)
    if match:
        post = attach_to_post(video, match)
        if campaign_id and not post.campaign_id:
            post.campaign_id = campaign_id
        return post

    if not allow_create:
        return None
    return create_post_for(video, campaign_id=campaign_id)


def regroup_ungrouped(campaign_id=None):
    """Group every campaign video that isn't in a post yet.

    Used for backfilling UGC that was added before posts existed. Processes
    oldest first so the earliest upload seeds the post.
    """
    from models import CampaignVideo

    q = db.session.query(Video).join(
        CampaignVideo, CampaignVideo.video_id == Video.id
    ).filter(Video.post_id.is_(None))
    if campaign_id:
        q = q.filter(CampaignVideo.campaign_id == campaign_id)
    videos = q.order_by(Video.created_at.asc(), Video.id.asc()).all()

    created, merged = 0, 0
    for video in videos:
        cid = campaign_id
        if cid is None:
            link = CampaignVideo.query.filter_by(video_id=video.id).first()
            cid = link.campaign_id if link else None
        match = find_matching_post(video)
        if match:
            attach_to_post(video, match)
            if cid and not match.campaign_id:
                match.campaign_id = cid
            merged += 1
        else:
            create_post_for(video, campaign_id=cid)
            created += 1
    db.session.commit()
    return {'created': created, 'merged': merged, 'total': len(videos)}


def cleanup_empty_posts():
    """Delete posts left with no videos after unlinking/merging."""
    empty = [p for p in Post.query.all() if not p.videos]
    for p in empty:
        db.session.delete(p)
    if empty:
        db.session.commit()
    return len(empty)


def merge_posts(source, target):
    """Move every video from source into target, then delete source."""
    if source.id == target.id:
        return target
    # Move through the relationship, not the raw FK: deleting a parent makes
    # SQLAlchemy null out the FK of anything still in its loaded collection,
    # which would silently orphan the videos we just moved.
    for video in list(source.videos):
        source.videos.remove(video)
        target.videos.append(video)
    if not target.notes and source.notes:
        target.notes = source.notes
    if not target.campaign_id and source.campaign_id:
        target.campaign_id = source.campaign_id
    db.session.delete(source)
    db.session.commit()
    return target
