import os
import json
import threading
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, Response, abort
from models import db, Video, Framework, Product, Campaign, DEFAULT_FRAMEWORKS, DEFAULT_PRODUCTS, _STALE_DEFAULT_FRAMEWORKS
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
    """Download and cache a thumbnail to disk."""
    import requests as req
    try:
        thumb_dir = os.path.join(_data_dir, 'thumbnails')
        os.makedirs(thumb_dir, exist_ok=True)
        r = req.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0 (compatible)'})
        if r.status_code == 200:
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
    ]
    with db.engine.connect() as conn:
        for table, col, col_type in new_columns:
            try:
                conn.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}'))
                conn.commit()
            except Exception:
                pass  # Column already exists


def seed_db():
    """Seed default frameworks and products if tables are empty."""
    # Remove stale default frameworks from the original seed
    stale = Framework.query.filter(Framework.name.in_(_STALE_DEFAULT_FRAMEWORKS)).all()
    for f in stale:
        db.session.delete(f)

    if Framework.query.count() == len(stale):
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
        return Response(r.content, mimetype=r.headers.get('content-type', 'image/jpeg'))
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

    query = Video.query
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
    return render_template('video_detail.html', video=video, statuses=CAMPAIGN_STATUSES)


@app.route('/videos/<int:id>/reanalyze', methods=['POST'])
def video_reanalyze(id):
    video = Video.query.get_or_404(id)
    video.analysis = None
    video.framework_id = None
    video.video_format = None
    db.session.commit()
    threading.Thread(target=classify_in_background, args=(id,), daemon=True).start()
    return Response('', status=204, headers={'HX-Redirect': f'/videos/{id}'})


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


@app.route('/campaigns', methods=['POST'])
def campaign_create():
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


@app.route('/campaigns')
def campaigns():
    product_filter = request.args.get('product_id', type=int)
    status_filter = request.args.get('status', '').strip()
    query = Campaign.query.order_by(Campaign.created_at.desc())
    if product_filter:
        query = query.filter_by(product_id=product_filter)
    if status_filter:
        query = query.filter_by(status=status_filter)
    campaigns = query.all()
    products = Product.query.order_by(Product.name).all()
    return render_template('campaigns.html', campaigns=campaigns, products=products,
                           product_filter=product_filter, status_filter=status_filter,
                           statuses=CAMPAIGN_STATUSES)


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
# Analytics
# ---------------------------------------------------------------------------

@app.route('/analytics')
def analytics():
    product_filter = request.args.get('product_id', type=int)
    query = Campaign.query.filter(Campaign.posted_at.isnot(None))
    if product_filter:
        query = query.filter_by(product_id=product_filter)
    posted = query.order_by(Campaign.views.desc().nullslast()).all()
    products = Product.query.order_by(Product.name).all()

    total_views = sum(c.views or 0 for c in posted)
    total_likes = sum(c.likes or 0 for c in posted)
    total_comments = sum(c.comments or 0 for c in posted)
    best = max(posted, key=lambda c: c.views or 0) if posted else None

    return render_template('analytics.html', posted=posted, products=products,
                           product_filter=product_filter, total_views=total_views,
                           total_likes=total_likes, total_comments=total_comments, best=best)


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

    # Fetch metadata + classify in background
    def process(video_id):
        with app.app_context():
            from ingest import fetch_metadata, fetch_rich_content, fetch_oembed
            v = Video.query.get(video_id)
            meta = fetch_metadata(v.url)
            duration = None
            if meta and 'error' not in meta:
                v.creator = meta.get('creator', '')
                v.title = meta.get('title', '')
                v.caption = meta.get('caption', '')
                v.thumbnail_url = meta.get('thumbnail_url', '')
                v.raw_metadata = meta.get('raw', '')
                duration = meta.get('duration')
                db.session.commit()
                if v.thumbnail_url:
                    _cache_thumbnail(v.id, v.thumbnail_url)
                embed = fetch_oembed(v.url, v.platform)
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

    threading.Thread(target=process, args=(video.id,), daemon=True).start()

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

threading.Thread(target=_run_backfill_embeds, daemon=True).start()
threading.Thread(target=_run_backfill_formats, daemon=True).start()

if __name__ == '__main__':
    app.run(port=5003, debug=True)
