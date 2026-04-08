import os
import threading
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, jsonify
from models import db, Video, Framework, Product, Campaign, DEFAULT_FRAMEWORKS, DEFAULT_PRODUCTS, _STALE_DEFAULT_FRAMEWORKS
import ai

app = Flask(__name__)

# Ensure data directory exists (needed for production SQLite path)
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///spark.db')
if _db_url.startswith('sqlite:////'):
    _db_dir = os.path.dirname(_db_url.replace('sqlite:////', '/'))
    os.makedirs(_db_dir, exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'spark-secret-key'

db.init_app(app)

# Initialize DB on startup (runs under both gunicorn and flask dev server)
with app.app_context():
    db.create_all()


def migrate_db():
    """Add new columns to existing tables without dropping data."""
    new_columns = [
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


def classify_in_background(video_id):
    """Run framework classification in a background thread."""
    with app.app_context():
        video = Video.query.get(video_id)
        if not video:
            return
        frameworks = Framework.query.all()
        framework_id, analysis = ai.classify_video(video, frameworks)
        if framework_id:
            video.framework_id = framework_id
        if analysis:
            video.analysis = analysis
        db.session.commit()


# ---------------------------------------------------------------------------
# Inspo
# ---------------------------------------------------------------------------

CAMPAIGN_STATUSES = ['Drafting', 'Pitching', 'Assigned', 'Complete']

@app.route('/')
def inspo():
    q = request.args.get('q', '').strip()
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
    videos = query.order_by(Video.created_at.desc()).all()
    frameworks = Framework.query.order_by(Framework.name).all()
    return render_template('inspo.html', videos=videos, frameworks=frameworks, q=q)


# ---------------------------------------------------------------------------
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

    from ingest import detect_platform, fetch_metadata

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
            v = Video.query.get(video_id)
            meta = fetch_metadata(v.url)
            if meta and 'error' not in meta:
                v.creator = meta.get('creator', '')
                v.title = meta.get('title', '')
                v.caption = meta.get('caption', '')
                v.thumbnail_url = meta.get('thumbnail_url', '')
                v.raw_metadata = meta.get('raw', '')
                db.session.commit()
            classify_in_background(video_id)

    threading.Thread(target=process, args=(video.id,), daemon=True).start()

    return jsonify({'status': 'created', 'id': video.id})


with app.app_context():
    migrate_db()
    seed_db()

if __name__ == '__main__':
    app.run(port=5003, debug=True)
