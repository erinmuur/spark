import os
import json
import threading
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, Response, abort
from models import db, Video, Framework, Product, Campaign, CampaignVideo, DEFAULT_FRAMEWORKS, DEFAULT_PRODUCTS, _STALE_DEFAULT_FRAMEWORKS
import ai

app = Flask(__name__)

# Ensure data directory exists (needed for production SQLite path)
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///spark.db')
if _db_url.startswith('sqlite:////'):
    _db_dir = os.path.dirname(_db_url.replace('sqlite:////', '/'))
    os.makedirs(_db_dir, exist_ok=True)
    _data_dir = _db_dir
else:
    _data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    os.makedirs(_data_dir, exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'spark-secret-key'

db.init_app(app)

# Initialize DB on startup (runs under both gunicorn and flask dev server)
with app.app_context():
    db.create_all()
    # Migrations for columns added after initial deploy
    from sqlalchemy import text, inspect as sa_inspect
    _inspector = sa_inspect(db.engine)
    _video_cols = [c['name'] for c in _inspector.get_columns('video')]
    with db.engine.connect() as _conn:
        if 'slack_message' not in _video_cols:
            _conn.execute(text('ALTER TABLE video ADD COLUMN slack_message TEXT'))
            _conn.commit()
        if 'follower_count' not in _video_cols:
            _conn.execute(text('ALTER TABLE video ADD COLUMN follower_count INTEGER'))
            _conn.commit()
        if 'slack_user' not in _video_cols:
            _conn.execute(text('ALTER TABLE video ADD COLUMN slack_user VARCHAR(200)'))
            _conn.commit()
        if 'slack_channel' not in _video_cols:
            _conn.execute(text('ALTER TABLE video ADD COLUMN slack_channel VARCHAR(100)'))
            _conn.commit()
        if 'slack_ts' not in _video_cols:
            _conn.execute(text('ALTER TABLE video ADD COLUMN slack_ts VARCHAR(50)'))
            _conn.commit()


@app.template_filter('dark_embed')
def dark_embed_filter(html):
    """Inject dark-theme attributes into platform embeds."""
    if not html:
        return html
    if 'twitter-tweet' in html:
        # data-width tells Twitter's widget to render at this exact pixel width,
        # preventing overflow into the container that causes white corner bleed
        html = html.replace('class="twitter-tweet"', 'class="twitter-tweet" data-theme="dark" data-width="280"')
    if 'tiktok-embed' in html:
        html = html.replace('class="tiktok-embed"', 'class="tiktok-embed" data-background-color="#181818"')
        # Remove hardcoded width constraints so CSS can control the size
        html = html.replace('max-width:605px;min-width:325px;', '')
        html = html.replace('max-width: 605px; min-width: 325px;', '')
        html = html.replace('max-width:605px; min-width:325px;', '')
    return html


@app.template_filter('twitter_handle')
def twitter_handle_filter(url):
    """Extract @username from a Twitter/X URL."""
    import re
    m = re.search(r'(?:twitter|x)\.com/([^/?]+)', url or '')
    return m.group(1) if m else ''


@app.template_filter('fromjson')
def fromjson_filter(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


@app.template_filter('humannum')
def humannum_filter(n):
    if n is None:
        return ''
    n = int(n)
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.0f}K'
    return str(n)


def _cache_thumbnail(video_id, url):
    """Download and cache a thumbnail to disk. Discards bad/empty responses."""
    import requests as req
    try:
        thumb_dir = os.path.join(_data_dir, 'thumbnails')
        os.makedirs(thumb_dir, exist_ok=True)
        r = req.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0 (compatible)'})
        content_type = r.headers.get('content-type', '')
        if r.status_code == 200 and content_type.startswith('image/') and len(r.content) > 2048:
            with open(os.path.join(thumb_dir, f'{video_id}.jpg'), 'wb') as f:
                f.write(r.content)
    except Exception:
        pass


def migrate_db():
    """Add new columns to existing tables without dropping data."""
    new_columns = [
        ('video', 'transcript', 'TEXT'),
        ('video', 'embed_html', 'TEXT'),
        ('video', 'favorited', 'BOOLEAN DEFAULT 0'),
        ('video', 'video_format', 'TEXT'),
        ('campaign', 'posted_url', 'TEXT'),
        ('campaign', 'posted_at', 'DATETIME'),
        ('campaign', 'views', 'INTEGER'),
        ('campaign', 'likes', 'INTEGER'),
        ('campaign', 'comments', 'INTEGER'),
        ('campaign', 'shares', 'INTEGER'),
        ('campaign', 'saves', 'INTEGER'),
        ('campaign', 'name', 'TEXT'),
        ('campaign', 'description', 'TEXT'),
        ('video', 'tribe_scores', 'TEXT'),
        ('video', 'tribe_suggestions', 'TEXT'),
        ('video', 'tribe_status', 'TEXT'),
    ]
    with db.engine.connect() as conn:
        for table, col, col_type in new_columns:
            try:
                conn.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}'))
                conn.commit()
            except Exception:
                pass  # Column already exists

    # Make video_id and product_id nullable (SQLite requires table recreation)
    _migrate_campaign_nullable_fks()


def _migrate_campaign_nullable_fks():
    """Recreate campaign table with nullable video_id and product_id."""
    with db.engine.connect() as conn:
        try:
            result = conn.execute(db.text("PRAGMA table_info(campaign)"))
            cols = {row[1]: row[3] for row in result}  # name -> notnull flag
            if cols.get('video_id') != 1:
                return  # Already nullable
            conn.execute(db.text("ALTER TABLE campaign RENAME TO campaign_old"))
            conn.execute(db.text("""
                CREATE TABLE campaign (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    video_id INTEGER REFERENCES video(id),
                    product_id INTEGER REFERENCES product(id),
                    context_notes TEXT, concept TEXT, hook TEXT,
                    script_outline TEXT, visual_notes TEXT, cta TEXT,
                    status VARCHAR(20) DEFAULT 'Drafting',
                    posted_url TEXT, posted_at DATETIME,
                    views INTEGER, likes INTEGER, comments INTEGER,
                    shares INTEGER, saves INTEGER,
                    created_at DATETIME, updated_at DATETIME
                )
            """))
            # Copy data — old table may not have name/description columns
            old_cols = list(cols.keys())
            select_parts = []
            for c in ['id', 'name', 'description', 'video_id', 'product_id',
                       'context_notes', 'concept', 'hook', 'script_outline',
                       'visual_notes', 'cta', 'status', 'posted_url', 'posted_at',
                       'views', 'likes', 'comments', 'shares', 'saves',
                       'created_at', 'updated_at']:
                select_parts.append(c if c in old_cols else 'NULL')
            conn.execute(db.text(
                f"INSERT INTO campaign SELECT {', '.join(select_parts)} FROM campaign_old"
            ))
            conn.execute(db.text("DROP TABLE campaign_old"))
            conn.commit()
        except Exception:
            pass


def backfill_campaign_videos():
    """Create CampaignVideo rows for legacy campaigns that use the old video_id FK."""
    campaigns = Campaign.query.filter(Campaign.video_id.isnot(None)).all()
    for c in campaigns:
        existing = CampaignVideo.query.filter_by(campaign_id=c.id, video_id=c.video_id).first()
        if not existing:
            cv = CampaignVideo(
                campaign_id=c.id,
                video_id=c.video_id,
                views=c.views,
                likes=c.likes,
                comments=c.comments,
                shares=c.shares,
                saves=c.saves,
                posted_url=c.posted_url,
                posted_at=c.posted_at,
            )
            db.session.add(cv)
    db.session.commit()


def seed_db():
    """Seed default frameworks and products, adding any that are missing."""
    for f in DEFAULT_FRAMEWORKS:
        if not Framework.query.filter_by(name=f['name']).first():
            db.session.add(Framework(**f))

    if Product.query.count() == 0:
        for p in DEFAULT_PRODUCTS:
            db.session.add(Product(**p))

    db.session.commit()


def classify_in_background(video_id, frames=None, transcript=None):
    """Run framework + format classification in a background thread."""
    with app.app_context():
        video = Video.query.get(video_id)
        if not video:
            return
        frameworks = Framework.query.all()
        framework_id, analysis, video_format = ai.classify_video(video, frameworks, frames=frames, transcript=transcript)
        if framework_id:
            video.framework_id = framework_id
        if analysis:
            video.analysis = analysis
        if video_format:
            video.video_format = video_format
        db.session.commit()


def _process_new_video(video_id):
    """Background: fetch metadata, embed, transcript, classify for a new video."""
    with app.app_context():
        from ingest import fetch_metadata, fetch_rich_content, fetch_oembed
        v = Video.query.get(video_id)
        if not v:
            return
        meta = fetch_metadata(v.url)
        duration = None
        if meta and 'error' not in meta:
            v.creator = meta.get('creator', '')
            v.title = meta.get('title', '')
            v.caption = meta.get('caption', '')
            v.thumbnail_url = meta.get('thumbnail_url', '')
            v.raw_metadata = meta.get('raw', '')
            if meta.get('follower_count') is not None:
                v.follower_count = meta['follower_count']
            duration = meta.get('duration')
            db.session.commit()
            # Populate metrics on any CampaignVideo rows linked to this video
            _apply_video_metrics(v.id, meta)
            if v.thumbnail_url:
                _cache_thumbnail(v.id, v.thumbnail_url)
            # Use embed from scraper fallback (e.g. Instagram image posts) or fetch via oEmbed
            embed = meta.get('embed_html') or fetch_oembed(v.url, v.platform)
            if embed:
                v.embed_html = embed
                db.session.commit()
        rich = fetch_rich_content(v.url, duration=duration)
        frames = rich.get('frames', [])
        transcript = rich.get('transcript', '')
        if transcript:
            v.transcript = transcript
            db.session.commit()
        classify_in_background(video_id, frames=frames, transcript=transcript)


def _apply_video_metrics(video_id, meta):
    """Write analytics from metadata dict to all CampaignVideo rows for this video."""
    cvs = CampaignVideo.query.filter_by(video_id=video_id).all()
    if not cvs:
        return
    views = meta.get('view_count')
    likes = meta.get('like_count')
    comments = meta.get('comment_count')
    shares = meta.get('share_count')
    saves = meta.get('save_count')
    for cv in cvs:
        if views is not None:
            cv.views = views
        if likes is not None:
            cv.likes = likes
        if comments is not None:
            cv.comments = comments
        if shares is not None:
            cv.shares = shares
        if saves is not None:
            cv.saves = saves
    db.session.commit()


def find_or_create_videos(urls):
    """Given a list of URLs, find existing Video records or create new ones."""
    from ingest import detect_platform, _clean_url
    videos = []
    new_video_ids = []
    to_reclassify = []
    for url in urls:
        url = _clean_url(url.strip())
        if not url:
            continue
        existing = Video.query.filter_by(url=url).first()
        if existing:
            videos.append(existing)
            # Queue classification for existing videos missing framework or format
            if not existing.framework_id or not existing.video_format:
                to_reclassify.append(existing.id)
        else:
            video = Video(url=url, platform=detect_platform(url))
            db.session.add(video)
            db.session.flush()
            videos.append(video)
            new_video_ids.append(video.id)
    db.session.commit()
    # Stagger thread launches to avoid simultaneous DB writes (StaleDataError)
    for i, video_id in enumerate(new_video_ids):
        threading.Timer(i * 4, lambda vid=video_id: _process_new_video(vid)).start()
    # Reclassify existing videos missing framework/format
    if to_reclassify:
        threading.Thread(target=_reclassify_batch, args=(to_reclassify,), daemon=True).start()
    return videos


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

@app.route('/thumbnails/<int:video_id>')
def thumbnail(video_id):
    thumb_path = os.path.join(_data_dir, 'thumbnails', f'{video_id}.jpg')
    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype='image/jpeg')
    # Proxy from stored URL as fallback (e.g. older videos without cached thumb)
    video = Video.query.get_or_404(video_id)
    if not video.thumbnail_url:
        abort(404)
    try:
        import requests as req
        r = req.get(video.thumbnail_url, timeout=8,
                    headers={'User-Agent': 'Mozilla/5.0 (compatible)'})
        ct = r.headers.get('content-type', '')
        if r.status_code == 200 and ct.startswith('image/') and len(r.content) > 2048:
            # Cache it so we don't re-proxy on every request
            _cache_thumbnail(video_id, video.thumbnail_url)
            return Response(r.content, mimetype=ct)
        abort(404)
    except Exception:
        abort(404)


# ---------------------------------------------------------------------------
# Inspo
# ---------------------------------------------------------------------------

CAMPAIGN_STATUSES = ['Drafting', 'Pitching', 'Assigned', 'Complete']

@app.route('/')
def inspo():
    q = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'added_desc')

    query = Video.query.filter(Video.slack_ts.isnot(None), Video.slack_ts != '')
    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(
                Video.creator.ilike(like),
                Video.caption.ilike(like),
                Video.title.ilike(like),
            )
        )

    videos = query.all()

    def _likes(v):
        try:
            return json.loads(v.raw_metadata or '{}').get('like_count') or 0
        except Exception:
            return 0

    def _upload_date(v):
        try:
            d = json.loads(v.raw_metadata or '{}').get('upload_date', '')
            if d:
                return d  # 'YYYYMMDD' — sorts correctly as string
        except Exception:
            pass
        return v.created_at.strftime('%Y%m%d') if v.created_at else '0'

    if sort == 'engagement_desc':
        videos.sort(key=_likes, reverse=True)
    elif sort == 'engagement_asc':
        videos.sort(key=_likes)
    elif sort == 'posted_desc':
        videos.sort(key=_upload_date, reverse=True)
    elif sort == 'posted_asc':
        videos.sort(key=_upload_date)
    elif sort == 'added_asc':
        videos.sort(key=lambda v: v.created_at or datetime.min)
    else:  # added_desc (default)
        videos.sort(key=lambda v: v.created_at or datetime.min, reverse=True)

    frameworks = Framework.query.order_by(Framework.name).all()
    return render_template('inspo.html', videos=videos, frameworks=frameworks, q=q, sort=sort)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Video detail / delete / favorite
# ---------------------------------------------------------------------------

@app.route('/videos/<int:id>')
def video_detail(id):
    video = Video.query.get_or_404(id)
    from_campaign = None
    campaign_id = request.args.get('campaign_id', type=int)
    if campaign_id:
        from_campaign = Campaign.query.get(campaign_id)
    return render_template('video_detail.html', video=video, statuses=CAMPAIGN_STATUSES, from_campaign=from_campaign)


@app.route('/videos/<int:id>/reanalyze', methods=['POST'])
def video_reanalyze(id):
    video = Video.query.get_or_404(id)
    frameworks = Framework.query.all()
    framework_id, analysis, video_format = ai.classify_video(video, frameworks)
    if framework_id:
        video.framework_id = framework_id
    if analysis:
        video.analysis = analysis
    if video_format:
        video.video_format = video_format
    db.session.commit()
    return Response('', status=204, headers={'HX-Redirect': f'/videos/{id}'})


@app.route('/videos/<int:id>/tribe', methods=['POST'])
def video_tribe(id):
    """Kick off in-app TRIBE v2 inference in a background thread."""
    video = Video.query.get_or_404(id)
    if video.tribe_status == 'running':
        return jsonify({'ok': False, 'error': 'Analysis already running'}), 409

    import tribe

    def _run():
        with app.app_context():
            tribe.run_inference(id)

    video.tribe_status = 'running'
    db.session.commit()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({'ok': True, 'status': 'running'})


@app.route('/videos/<int:id>/tribe-status')
def video_tribe_status(id):
    """Poll endpoint — returns current tribe_status and scores/suggestions if done."""
    video = Video.query.get_or_404(id)
    status = video.tribe_status or 'idle'
    resp = {'status': status}
    if status == 'done':
        resp['scores'] = json.loads(video.tribe_scores) if video.tribe_scores else []
        resp['suggestions'] = video.tribe_suggestions or ''
    return jsonify(resp)


@app.route('/videos/<int:id>/delete', methods=['POST'])
def video_delete(id):
    video = Video.query.get_or_404(id)
    # Remove cached thumbnail if present
    thumb_path = os.path.join(_data_dir, 'thumbnails', f'{id}.jpg')
    if os.path.exists(thumb_path):
        os.remove(thumb_path)
    db.session.delete(video)
    db.session.commit()
    return redirect(url_for('inspo'))


@app.route('/admin/env-debug')
def admin_env_debug():
    import subprocess
    modal_ver = subprocess.run(['pip', 'show', 'modal'], capture_output=True, text=True).stdout
    uvx_path = subprocess.run(['which', 'uvx'], capture_output=True, text=True).stdout.strip() or 'not found'
    routes = sorted([str(r) for r in app.url_map.iter_rules() if 'admin' in str(r)])
    return jsonify({'modal': modal_ver, 'uvx': uvx_path, 'admin_routes': routes})


@app.route('/admin/video-debug')
def admin_video_debug():
    """Temp: show raw_metadata for all videos."""
    videos = Video.query.all()
    out = []
    for v in videos:
        raw = {}
        if v.raw_metadata:
            try:
                raw = json.loads(v.raw_metadata)
            except Exception:
                raw = {'parse_error': True}
        out.append({'id': v.id, 'url': v.url, 'creator': v.creator, 'raw_keys': list(raw.keys()), 'saves_fields': {k: raw.get(k) for k in ['digg_count', 'save_count', 'collectCount', 'savedCount']}})
    return jsonify(out)


@app.route('/admin/retry-failed-videos', methods=['POST'])
def admin_retry_failed_videos():
    """Re-trigger metadata fetch for videos that failed (no raw_metadata)."""
    failed = Video.query.filter(Video.raw_metadata.is_(None)).all()
    count = len(failed)
    for i, v in enumerate(failed):
        threading.Timer(i * 4, lambda vid=v.id: _process_new_video(vid)).start()
    return jsonify({'retrying': count, 'ids': [v.id for v in failed]})


@app.route('/admin/retry-missing-thumbnails', methods=['POST'])
def admin_retry_missing_thumbnails():
    """Re-trigger metadata fetch for videos that have data but no thumbnail."""
    from sqlalchemy import or_
    missing = Video.query.filter(
        Video.creator.isnot(None),
        or_(Video.thumbnail_url.is_(None), Video.thumbnail_url == ''),
    ).all()
    count = len(missing)
    for i, v in enumerate(missing):
        threading.Timer(i * 4, lambda vid=v.id: _process_new_video(vid)).start()
    return jsonify({'retrying': count, 'ids': [v.id for v in missing]})


@app.route('/admin/backfill-followers', methods=['POST'])
def admin_backfill_followers():
    """Re-fetch Apify data for TikTok/Instagram videos missing follower counts (async)."""
    def _do_backfill(ids):
        from ingest import _fetch_tiktok_via_apify, _fetch_instagram_via_apify
        with app.app_context():
            updated = 0
            for vid_id in ids:
                v = Video.query.get(vid_id)
                if not v:
                    continue
                try:
                    if v.platform == 'tiktok':
                        data = _fetch_tiktok_via_apify(v.url)
                    else:
                        data = _fetch_instagram_via_apify(v.url)
                    if data and data.get('follower_count') is not None:
                        v.follower_count = data['follower_count']
                        db.session.commit()
                        updated += 1
                except Exception:
                    pass
            logger.info(f'Follower backfill complete: {updated}/{len(ids)} updated')

    ids = [row.id for row in Video.query.filter(
        Video.follower_count.is_(None),
        Video.platform.in_(['tiktok', 'instagram']),
    ).with_entities(Video.id).all()]
    threading.Thread(target=_do_backfill, args=(ids,), daemon=True).start()
    return jsonify({'queued': len(ids), 'message': 'Backfill running in background'})


@app.route('/admin/fix-thumbnail/<int:video_id>', methods=['POST'])
def admin_fix_thumbnail(video_id):
    """Re-fetch thumbnail for a specific video via fresh metadata call."""
    v = Video.query.get_or_404(video_id)
    # Delete any stale cached file first
    thumb_path = os.path.join(_data_dir, 'thumbnails', f'{video_id}.jpg')
    if os.path.exists(thumb_path):
        os.remove(thumb_path)
    threading.Thread(target=_process_new_video, args=(video_id,), daemon=True).start()
    return jsonify({'status': 'retrying', 'id': video_id})


@app.route('/admin/purge-bad-thumbnails', methods=['POST'])
def admin_purge_bad_thumbnails():
    """Delete cached thumbnails that are too small to be real images, then re-fetch."""
    thumb_dir = os.path.join(_data_dir, 'thumbnails')
    purged = []
    if os.path.isdir(thumb_dir):
        for fname in os.listdir(thumb_dir):
            fpath = os.path.join(thumb_dir, fname)
            if os.path.getsize(fpath) < 2048:
                os.remove(fpath)
                try:
                    vid_id = int(fname.replace('.jpg', ''))
                    purged.append(vid_id)
                except ValueError:
                    pass
    # Re-trigger full metadata fetch for affected videos
    for i, vid_id in enumerate(purged):
        threading.Timer(i * 4, lambda vid=vid_id: _process_new_video(vid)).start()
    return jsonify({'purged': purged, 'retrying': len(purged)})


@app.route('/admin/apify-debug')
def admin_apify_debug():
    """Temp: run Apify scraper on first video and return full raw item."""
    v = Video.query.first()
    if not v:
        return jsonify({'error': 'no videos'})
    api_token = os.environ.get('APIFY_API_TOKEN')
    if not api_token:
        return jsonify({'error': 'no APIFY_API_TOKEN'})
    try:
        from apify_client import ApifyClient
        client = ApifyClient(api_token)
        if v.platform == 'instagram':
            run = client.actor('apify/instagram-scraper').call(run_input={'directUrls': [v.url], 'resultsLimit': 1, 'resultsType': 'posts'}, timeout_secs=60)
            share_keys = ['sharesCount', 'reshareCount', 'repostsCount', 'videoSharesCount']
        else:
            run = client.actor('clockworks/tiktok-scraper').call(run_input={'postURLs': [v.url], 'resultsPerPage': 1}, timeout_secs=60)
            share_keys = ['shareCount', 'collectCount']
        items = list(client.dataset(run['defaultDatasetId']).iterate_items())
        item = items[0] if items else {}
        return jsonify({'platform': v.platform, 'status': run.get('status'), 'item_keys': list(item.keys()), 'share_fields': {k: item.get(k) for k in share_keys}})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/admin/clear-tribe-errors', methods=['POST'])
def admin_clear_tribe_errors():
    """Reset all failed/errored TRIBE statuses back to idle."""
    from sqlalchemy import or_
    videos = Video.query.filter(
        or_(Video.tribe_status.like('error:%'), Video.tribe_status == 'running')
    ).all()
    count = len(videos)
    for v in videos:
        v.tribe_status = None
    db.session.commit()
    return jsonify({'cleared': count})


@app.route('/admin/delete-all-videos', methods=['POST'])
def admin_delete_all_videos():
    """One-shot: delete all videos and their thumbnails."""
    videos = Video.query.all()
    count = len(videos)
    for v in videos:
        thumb_path = os.path.join(_data_dir, 'thumbnails', f'{v.id}.jpg')
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        db.session.delete(v)
    db.session.commit()
    return jsonify({'deleted': count})


@app.route('/admin/delete-blank-videos', methods=['POST'])
def admin_delete_blank_videos():
    """One-shot: delete videos with no thumbnail and no creator (failed ingests)."""
    blanks = Video.query.filter(
        Video.thumbnail_url.is_(None),
        Video.creator.is_(None),
    ).all()
    count = len(blanks)
    for v in blanks:
        thumb_path = os.path.join(_data_dir, 'thumbnails', f'{v.id}.jpg')
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        db.session.delete(v)
    db.session.commit()
    return jsonify({'deleted': count})


@app.route('/videos/<int:id>/favorite', methods=['POST'])
def video_favorite(id):
    video = Video.query.get_or_404(id)
    video.favorited = not video.favorited
    db.session.commit()
    if request.headers.get('HX-Request'):
        heart = '♥' if video.favorited else '♡'
        cls = 'fav-btn active' if video.favorited else 'fav-btn'
        return f'<button class="{cls}" hx-post="/videos/{id}/favorite" hx-target="this" hx-swap="outerHTML">{heart}</button>'
    return redirect(url_for('inspo'))


# ---------------------------------------------------------------------------
# Settings (combines Products + Frameworks)
# ---------------------------------------------------------------------------

@app.route('/settings')
def settings():
    products = Product.query.order_by(Product.name).all()
    frameworks = Framework.query.order_by(Framework.created_at).all()
    return render_template('settings.html', products=products, frameworks=frameworks)


# ---------------------------------------------------------------------------
# Frameworks
# ---------------------------------------------------------------------------

@app.route('/frameworks')
def frameworks():
    return redirect(url_for('settings'))


@app.route('/frameworks/new', methods=['POST'])
def framework_create():
    f = Framework(
        name=request.form['name'],
        description=request.form.get('description', ''),
        structure=request.form.get('structure', ''),
        example_hooks=request.form.get('example_hooks', ''),
    )
    db.session.add(f)
    db.session.commit()
    return redirect(url_for('settings'))


@app.route('/frameworks/<int:id>/edit', methods=['POST'])
def framework_edit(id):
    f = Framework.query.get_or_404(id)
    f.name = request.form['name']
    f.description = request.form.get('description', '')
    f.structure = request.form.get('structure', '')
    f.example_hooks = request.form.get('example_hooks', '')
    db.session.commit()
    return redirect(url_for('settings'))


@app.route('/frameworks/<int:id>/delete', methods=['POST'])
def framework_delete(id):
    f = Framework.query.get_or_404(id)
    db.session.delete(f)
    db.session.commit()
    return redirect(url_for('settings'))


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@app.route('/products')
def products():
    return redirect(url_for('settings'))


@app.route('/products/<int:id>/edit', methods=['POST'])
def product_edit(id):
    p = Product.query.get_or_404(id)
    p.description = request.form.get('description', '')
    p.key_benefits = request.form.get('key_benefits', '')
    p.target_audience = request.form.get('target_audience', '')
    p.usp = request.form.get('usp', '')
    p.voice_notes = request.form.get('voice_notes', '')
    db.session.commit()

    if request.headers.get('HX-Request'):
        return '<span class="saved-notice">Saved ✓</span>'
    return redirect(url_for('products'))


# ---------------------------------------------------------------------------
# Campaigns — modal form (HTMX)
# ---------------------------------------------------------------------------

@app.route('/campaigns/new')
def campaign_new():
    video_id = request.args.get('video_id', type=int)
    video = Video.query.get_or_404(video_id)
    products = Product.query.order_by(Product.name).all()
    return render_template('partials/campaign_modal.html', video=video, products=products)


@app.route('/campaigns', methods=['GET', 'POST'])
def campaigns():
    if request.method == 'GET':
        return _campaigns_list()
    return _campaign_create_post()


def _campaign_create_post():
    # Detect which flow: legacy (from Inspo modal) vs new (from /campaigns/create)
    if request.form.get('video_id'):
        # LEGACY FLOW: single video + AI brief
        video_id = request.form.get('video_id', type=int)
        product_id = request.form.get('product_id', type=int)
        context_notes = request.form.get('context_notes', '')

        video = Video.query.get_or_404(video_id)
        product = Product.query.get_or_404(product_id)
        framework = video.framework

        brief = ai.generate_campaign(video, framework, product, context_notes)

        campaign = Campaign(
            video_id=video_id,
            product_id=product_id,
            context_notes=context_notes,
            concept=brief.get('concept', ''),
            hook=brief.get('hook', ''),
            script_outline=brief.get('script_outline', ''),
            visual_notes=brief.get('visual_notes', ''),
            cta=brief.get('cta', ''),
        )
        db.session.add(campaign)
        db.session.commit()

        if request.headers.get('HX-Request'):
            return '', 204, {'HX-Redirect': url_for('campaign_detail', id=campaign.id)}
        return redirect(url_for('campaign_detail', id=campaign.id))
    else:
        # NEW FLOW: multi-video campaign from /campaigns/create
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        video_urls_raw = request.form.get('video_urls', '')
        product_id = request.form.get('product_id', type=int)

        # Parse URLs
        from ingest import extract_video_urls
        urls = extract_video_urls(video_urls_raw)
        if not urls:
            urls = [u.strip() for u in video_urls_raw.split('\n') if u.strip()]

        videos = find_or_create_videos(urls)

        campaign = Campaign(
            name=name,
            description=description,
            product_id=product_id if product_id else None,
            status='Drafting',
        )
        db.session.add(campaign)
        db.session.flush()

        for video in videos:
            cv = CampaignVideo(campaign_id=campaign.id, video_id=video.id)
            db.session.add(cv)
            # Pre-populate metrics from existing raw_metadata if available
            if video.raw_metadata:
                try:
                    raw = json.loads(video.raw_metadata)
                    cv.views = raw.get('view_count')
                    cv.likes = raw.get('like_count')
                    cv.comments = raw.get('comment_count')
                    cv.shares = raw.get('repost_count')
                    cv.saves = raw.get('digg_count')
                except (json.JSONDecodeError, TypeError):
                    pass

        db.session.commit()
        return redirect(url_for('campaign_detail', id=campaign.id))


@app.route('/campaigns/create')
def campaign_create_form():
    products = Product.query.order_by(Product.name).all()
    return render_template('campaign_create.html', products=products)


def _campaigns_list():
    status_filter = request.args.get('status', '').strip()
    query = Campaign.query.order_by(Campaign.created_at.desc())
    if status_filter:
        query = query.filter_by(status=status_filter)
    all_campaigns = query.all()

    # Aggregate metrics across all campaign_videos
    all_cvs = CampaignVideo.query.all()
    total_views = sum(cv.views or 0 for cv in all_cvs)
    total_likes = sum(cv.likes or 0 for cv in all_cvs)
    total_comments = sum(cv.comments or 0 for cv in all_cvs)
    total_saves = sum(cv.saves or 0 for cv in all_cvs)

    products = Product.query.order_by(Product.name).all()
    return render_template('campaigns.html', campaigns=all_campaigns, products=products,
                           status_filter=status_filter, statuses=CAMPAIGN_STATUSES,
                           total_views=total_views, total_likes=total_likes,
                           total_comments=total_comments, total_saves=total_saves)


@app.route('/campaigns/<int:id>')
def campaign_detail(id):
    campaign = Campaign.query.get_or_404(id)
    products = Product.query.order_by(Product.name).all()
    return render_template('campaign_detail.html', campaign=campaign, products=products,
                           statuses=CAMPAIGN_STATUSES)


@app.route('/campaigns/<int:id>/edit', methods=['POST'])
def campaign_edit(id):
    campaign = Campaign.query.get_or_404(id)
    campaign.concept = request.form.get('concept', '')
    campaign.hook = request.form.get('hook', '')
    campaign.script_outline = request.form.get('script_outline', '')
    campaign.visual_notes = request.form.get('visual_notes', '')
    campaign.cta = request.form.get('cta', '')
    campaign.context_notes = request.form.get('context_notes', '')
    campaign.status = request.form.get('status', campaign.status)
    campaign.updated_at = datetime.utcnow()
    db.session.commit()

    if request.headers.get('HX-Request'):
        return '<span class="saved-notice">Saved ✓</span>'
    return redirect(url_for('campaign_detail', id=id))


@app.route('/campaigns/<int:id>/regenerate', methods=['POST'])
def campaign_regenerate(id):
    campaign = Campaign.query.get_or_404(id)
    context_notes = request.form.get('context_notes', campaign.context_notes or '')

    video = campaign.video
    product = campaign.product
    framework = video.framework

    brief = ai.generate_campaign(video, framework, product, context_notes)

    campaign.concept = brief.get('concept', '')
    campaign.hook = brief.get('hook', '')
    campaign.script_outline = brief.get('script_outline', '')
    campaign.visual_notes = brief.get('visual_notes', '')
    campaign.cta = brief.get('cta', '')
    campaign.context_notes = context_notes
    campaign.updated_at = datetime.utcnow()
    db.session.commit()

    return redirect(url_for('campaign_detail', id=id))


@app.route('/campaigns/<int:id>/status', methods=['POST'])
def campaign_status(id):
    campaign = Campaign.query.get_or_404(id)
    new_status = request.form.get('status', campaign.status)
    if new_status in CAMPAIGN_STATUSES:
        campaign.status = new_status
    campaign.updated_at = datetime.utcnow()
    db.session.commit()

    if request.headers.get('HX-Request'):
        options = ''.join(
            f'<option value="{s}" {"selected" if s == campaign.status else ""}>{s}</option>'
            for s in CAMPAIGN_STATUSES
        )
        return f'''
        <span class="status-badge status-{campaign.status.lower()}">{campaign.status}</span>
        <select hx-post="/campaigns/{id}/status" hx-target="#status-area"
                hx-swap="innerHTML" name="status" hx-trigger="change" style="width:auto;margin:0;">
          {options}
        </select>
        '''
    return redirect(url_for('campaign_detail', id=id))


# ---------------------------------------------------------------------------
# Campaign Chat (streaming)
# ---------------------------------------------------------------------------

@app.route('/campaigns/<int:id>/chat', methods=['POST'])
def campaign_chat(id):
    campaign = Campaign.query.get_or_404(id)
    data = request.get_json()
    user_message = data.get('message', '').strip()
    history = data.get('history', [])

    if not user_message:
        return jsonify({'error': 'empty message'}), 400

    # Build system prompt with campaign context
    product_name = campaign.product.name if campaign.product else 'N/A'
    lines = [
        'You are a campaign analyst for a short-form video marketing team. '
        'Answer questions about this campaign using the data below. '
        'Write in plain prose — no markdown, no bullet points, no headers, no asterisks. '
        'Structure your response as short paragraphs of 2–4 sentences each, separated by blank lines. '
        'Each paragraph should cover a single creator or idea — never cram multiple creators into one paragraph. '
        'Every time you mention a video or its stats, you MUST identify the creator '
        'by their @handle (e.g. @creatorname). Never refer to a video without naming the creator. '
        'When asked about engagement relative to audience size or follower counts, use engagement '
        'ratios (saves÷views, likes÷views, comments÷views) as the proxy — these reveal which '
        'creators punched above their weight. Never mention missing data or apologize for what '
        'you do not have. Just answer using what is available.',
        '',
        f'Campaign: {campaign.display_name}',
        f'Description: {campaign.description or "N/A"}',
        f'Status: {campaign.status}',
        f'Product: {product_name}',
        '',
        f'Videos in this campaign ({len(campaign.campaign_videos)} total):',
    ]
    for i, cv in enumerate(campaign.campaign_videos, 1):
        v = cv.video
        fw_name = v.framework.name if v.framework else 'Unclassified'
        # Pull original video stats from raw_metadata as fallback for campaign metrics
        raw = {}
        if v.raw_metadata:
            try:
                raw = json.loads(v.raw_metadata)
            except Exception:
                pass
        orig_views = raw.get('view_count') or cv.views or 0
        orig_likes = raw.get('like_count') or cv.likes or 0
        orig_comments = raw.get('comment_count') or cv.comments or 0
        orig_shares = raw.get('repost_count') or raw.get('share_count') or cv.shares or 0
        orig_saves = raw.get('digg_count') or cv.saves or 0
        lines.append(
            f'{i}. "{v.title or "Untitled"}" by @{v.creator or "unknown"} ({v.platform or "?"})'
        )
        lines.append(f'   Framework: {fw_name} | Format: {v.video_format or "N/A"}')
        if v.follower_count:
            lines.append(f'   Creator followers: {v.follower_count:,}')
        lines.append(
            f'   Video stats: {orig_views:,} views, {orig_likes:,} likes, '
            f'{orig_comments:,} comments, {orig_shares:,} shares, {orig_saves:,} saves'
        )
        if v.analysis:
            lines.append(f'   Analysis: {v.analysis}')

    system_prompt = '\n'.join(lines)

    def generate():
        try:
            for chunk in ai.chat_campaign_stream(system_prompt, user_message, history):
                # Encode newlines so SSE framing isn't broken; client decodes them
                safe = chunk.replace('\n', '\\n')
                yield f"data: {safe}\n\n"
        except Exception as e:
            yield f"data: [ERROR] {e}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@app.route('/analytics')
def analytics():
    all_campaigns = Campaign.query.order_by(Campaign.created_at.desc()).all()

    # Aggregate metrics across all campaign_videos
    all_cvs = CampaignVideo.query.all()
    total_views = sum(cv.views or 0 for cv in all_cvs)
    total_likes = sum(cv.likes or 0 for cv in all_cvs)
    total_comments = sum(cv.comments or 0 for cv in all_cvs)
    total_saves = sum(cv.saves or 0 for cv in all_cvs)

    return render_template('analytics.html', campaigns=all_campaigns,
                           total_views=total_views, total_likes=total_likes,
                           total_comments=total_comments, total_saves=total_saves,
                           active_tab='campaigns')


@app.route('/analytics/frameworks')
def analytics_frameworks():
    from collections import defaultdict
    all_cvs = CampaignVideo.query.all()

    # Group by framework name
    fw_data = defaultdict(lambda: {'description': '', 'views': 0, 'likes': 0, 'comments': 0, 'shares': 0, 'saves': 0, 'video_count': 0})
    for cv in all_cvs:
        video = cv.video
        fw_name = video.framework.name if video.framework else 'Unclassified'
        fw_desc = video.framework.description if video.framework else ''
        bucket = fw_data[fw_name]
        bucket['description'] = fw_desc
        bucket['views'] += cv.views or 0
        bucket['likes'] += cv.likes or 0
        bucket['comments'] += cv.comments or 0
        bucket['shares'] += cv.shares or 0
        bucket['saves'] += cv.saves or 0
        bucket['video_count'] += 1

    class FrameworkRow:
        def __init__(self, name, data):
            self.name = name
            self.description = data['description']
            self.views = data['views']
            self.likes = data['likes']
            self.comments = data['comments']
            self.shares = data['shares']
            self.saves = data['saves']
            self.video_count = data['video_count']

    framework_rows = [FrameworkRow(name, data) for name, data in sorted(fw_data.items())]
    total_views = sum(r.views for r in framework_rows)
    total_likes = sum(r.likes for r in framework_rows)
    total_comments = sum(r.comments for r in framework_rows)
    total_saves = sum(r.saves for r in framework_rows)

    return render_template('analytics_frameworks.html',
                           framework_rows=framework_rows,
                           total_views=total_views, total_likes=total_likes,
                           total_comments=total_comments, total_saves=total_saves,
                           active_tab='frameworks')


@app.route('/analytics/creators')
def analytics_creators():
    from collections import defaultdict
    all_cvs = CampaignVideo.query.all()

    # Group by (creator, platform)
    cr_data = defaultdict(lambda: {'views': 0, 'likes': 0, 'comments': 0, 'shares': 0, 'saves': 0, 'video_count': 0})
    for cv in all_cvs:
        video = cv.video
        creator = video.creator or 'Unknown'
        platform = video.platform or 'unknown'
        bucket = cr_data[(creator, platform)]
        bucket['views'] += cv.views or 0
        bucket['likes'] += cv.likes or 0
        bucket['comments'] += cv.comments or 0
        bucket['shares'] += cv.shares or 0
        bucket['saves'] += cv.saves or 0
        bucket['video_count'] += 1

    class CreatorRow:
        def __init__(self, key, data):
            self.creator = key[0]
            self.platform = key[1]
            self.views = data['views']
            self.likes = data['likes']
            self.comments = data['comments']
            self.shares = data['shares']
            self.saves = data['saves']
            self.video_count = data['video_count']

    creator_rows = [CreatorRow(key, data) for key, data in sorted(cr_data.items())]
    total_views = sum(r.views for r in creator_rows)
    total_likes = sum(r.likes for r in creator_rows)
    total_comments = sum(r.comments for r in creator_rows)
    total_saves = sum(r.saves for r in creator_rows)

    return render_template('analytics_creators.html',
                           creator_rows=creator_rows,
                           total_views=total_views, total_likes=total_likes,
                           total_comments=total_comments, total_saves=total_saves,
                           active_tab='creators')


@app.route('/campaigns/<int:campaign_id>/add-videos', methods=['POST'])
def campaign_add_videos(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    video_urls_raw = request.form.get('video_urls', '')

    from ingest import extract_video_urls
    urls = extract_video_urls(video_urls_raw)
    if not urls:
        urls = [u.strip() for u in video_urls_raw.split('\n') if u.strip()]

    videos = find_or_create_videos(urls)
    added = 0
    for video in videos:
        exists = CampaignVideo.query.filter_by(campaign_id=campaign.id, video_id=video.id).first()
        if not exists:
            cv = CampaignVideo(campaign_id=campaign.id, video_id=video.id)
            # Pre-populate metrics from existing raw_metadata if available
            if video.raw_metadata:
                try:
                    raw = json.loads(video.raw_metadata)
                    cv.views = raw.get('view_count')
                    cv.likes = raw.get('like_count')
                    cv.comments = raw.get('comment_count')
                    cv.shares = raw.get('repost_count')
                    cv.saves = raw.get('digg_count')
                except (json.JSONDecodeError, TypeError):
                    pass
            db.session.add(cv)
            added += 1
    db.session.commit()

    if request.headers.get('HX-Request'):
        return '', 204, {'HX-Redirect': url_for('campaign_detail', id=campaign_id)}
    return redirect(url_for('campaign_detail', id=campaign_id))


def _reclassify_batch(video_ids):
    """Classify a batch of videos sequentially using existing metadata (no media download)."""
    import sys
    print(f"[reclassify] Starting batch for {len(video_ids)} videos: {video_ids}", file=sys.stderr, flush=True)
    try:
        with app.app_context():
            for vid in video_ids:
                try:
                    video = Video.query.get(vid)
                    if not video:
                        print(f"[reclassify] Video {vid} not found, skipping", file=sys.stderr, flush=True)
                        continue
                    frameworks = Framework.query.all()
                    print(f"[reclassify] Classifying video {vid} against {len(frameworks)} frameworks...", file=sys.stderr, flush=True)
                    framework_id, analysis, video_format = ai.classify_video(
                        video, frameworks, transcript=video.transcript
                    )
                    print(f"[reclassify] Video {vid}: raw result framework={framework_id}, analysis={analysis!r:.200}, format={video_format}", file=sys.stderr, flush=True)
                    if framework_id:
                        video.framework_id = framework_id
                    if analysis:
                        video.analysis = analysis
                    if video_format:
                        video.video_format = video_format
                    db.session.commit()
                except Exception as e:
                    print(f"[reclassify] Error processing video {vid}: {e}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[reclassify] Fatal error: {e}", file=sys.stderr, flush=True)


@app.route('/campaigns/<int:campaign_id>/reclassify', methods=['POST'])
def campaign_reclassify(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    video_ids = [
        cv.video.id for cv in campaign.campaign_videos
        if not cv.video.framework_id or not cv.video.video_format
    ]
    if video_ids:
        threading.Thread(target=_reclassify_batch, args=(video_ids,), daemon=True).start()
    if request.headers.get('HX-Request'):
        if video_ids:
            n = len(video_ids)
            return f'<span style="font-size:0.78rem; color:var(--text-tertiary);">Reclassifying {n} video{"s" if n != 1 else ""}… refresh in ~{n * 15}s</span>'
        return '<span style="font-size:0.78rem; color:var(--text-tertiary);">All videos already classified</span>'
    return redirect(url_for('campaign_detail', id=campaign_id))


@app.route('/campaigns/<int:campaign_id>/videos/<int:video_id>/metrics', methods=['POST'])
def campaign_video_metrics(campaign_id, video_id):
    cv = CampaignVideo.query.filter_by(campaign_id=campaign_id, video_id=video_id).first_or_404()

    def intval(key):
        v = request.form.get(key, '').strip()
        return int(v) if v.isdigit() else None

    cv.views = intval('views')
    cv.likes = intval('likes')
    cv.comments = intval('comments')
    cv.shares = intval('shares')
    cv.saves = intval('saves')
    db.session.commit()

    if request.headers.get('HX-Request'):
        return '<span class="saved-notice">Metrics saved ✓</span>'
    return redirect(url_for('campaign_detail', id=campaign_id))


@app.route('/campaigns/<int:id>/metrics', methods=['POST'])
def campaign_metrics(id):
    campaign = Campaign.query.get_or_404(id)
    campaign.posted_url = request.form.get('posted_url', '').strip() or None
    posted_at_str = request.form.get('posted_at', '').strip()
    if posted_at_str:
        try:
            campaign.posted_at = datetime.strptime(posted_at_str, '%Y-%m-%d')
        except ValueError:
            pass

    def intval(key):
        v = request.form.get(key, '').strip()
        return int(v) if v.isdigit() else None

    campaign.views = intval('views')
    campaign.likes = intval('likes')
    campaign.comments = intval('comments')
    campaign.shares = intval('shares')
    campaign.saves = intval('saves')
    campaign.updated_at = datetime.utcnow()
    db.session.commit()

    if request.headers.get('HX-Request'):
        return '<span class="saved-notice">Metrics saved ✓</span>'
    return redirect(url_for('campaign_detail', id=id))


# ---------------------------------------------------------------------------
# Internal: ingest endpoint (used by bot.py via direct DB access instead)
# ---------------------------------------------------------------------------

@app.route('/ingest', methods=['POST'])
def ingest():
    """Called internally by the bot to add a video and trigger classification."""
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'no url'}), 400

    from ingest import detect_platform

    existing = Video.query.filter_by(url=url).first()
    if existing:
        return jsonify({'status': 'exists', 'id': existing.id})

    video = Video(
        url=url,
        platform=detect_platform(url),
        slack_user=data.get('slack_user', ''),
        slack_channel=data.get('slack_channel', ''),
        slack_ts=data.get('slack_ts', ''),
    )
    db.session.add(video)
    db.session.commit()

    # Fetch metadata + metrics + classify in background
    threading.Thread(target=_process_new_video, args=(video.id,), daemon=True).start()

    return jsonify({'status': 'created', 'id': video.id})


def backfill_embeds():
    """Fetch oEmbed HTML for any existing videos that don't have it yet."""
    from ingest import fetch_oembed
    videos = Video.query.filter(
        Video.embed_html.is_(None),
        Video.platform.in_(['twitter', 'tiktok'])
    ).all()
    for v in videos:
        embed = fetch_oembed(v.url, v.platform)
        if embed:
            v.embed_html = embed
    if videos:
        db.session.commit()


def _run_backfill_embeds():
    with app.app_context():
        backfill_embeds()


def _run_backfill_formats():
    """Classify UGC format for any existing videos that don't have one yet."""
    with app.app_context():
        videos = Video.query.filter(Video.video_format.is_(None)).all()
        for v in videos:
            fmt = ai.classify_format(v)
            if fmt:
                v.video_format = fmt
        if videos:
            db.session.commit()


with app.app_context():
    migrate_db()
    seed_db()
    backfill_campaign_videos()

threading.Thread(target=_run_backfill_embeds, daemon=True).start()
threading.Thread(target=_run_backfill_formats, daemon=True).start()

if __name__ == '__main__':
    app.run(port=5003, debug=True)
