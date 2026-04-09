from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Framework(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    structure = db.Column(db.Text)
    example_hooks = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    videos = db.relationship('Video', backref='framework', lazy=True)


class Video(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String, nullable=False, unique=True)
    platform = db.Column(db.String(50))   # tiktok, instagram, twitter
    creator = db.Column(db.String(200))
    title = db.Column(db.String(500))
    caption = db.Column(db.Text)
    thumbnail_url = db.Column(db.String)
    framework_id = db.Column(db.Integer, db.ForeignKey('framework.id'))
    analysis = db.Column(db.Text)          # Claude's framework analysis
    transcript = db.Column(db.Text)        # Whisper speech-to-text
    embed_html = db.Column(db.Text)        # oEmbed HTML from platform
    raw_metadata = db.Column(db.Text)      # JSON from yt-dlp
    slack_user = db.Column(db.String(100))
    slack_channel = db.Column(db.String(100))
    slack_ts = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    favorited = db.Column(db.Boolean, default=False)
    video_format = db.Column(db.String(50))  # POV, Talking Head, etc.

    campaigns = db.relationship('Campaign', backref='video', lazy=True, cascade='all, delete-orphan')


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)  # Readwise, Reader, Bookwise
    description = db.Column(db.Text)
    key_benefits = db.Column(db.Text)
    target_audience = db.Column(db.Text)
    usp = db.Column(db.Text)
    voice_notes = db.Column(db.Text)

    campaigns = db.relationship('Campaign', backref='product', lazy=True)


class CampaignVideo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)
    views = db.Column(db.Integer)
    likes = db.Column(db.Integer)
    comments = db.Column(db.Integer)
    shares = db.Column(db.Integer)
    saves = db.Column(db.Integer)
    posted_url = db.Column(db.String)
    posted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    campaign = db.relationship('Campaign', backref=db.backref('campaign_videos', lazy=True, cascade='all, delete-orphan'))
    video = db.relationship('Video', backref=db.backref('campaign_video_links', lazy=True))

    __table_args__ = (db.UniqueConstraint('campaign_id', 'video_id', name='uq_campaign_video'),)


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200))
    description = db.Column(db.Text)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    context_notes = db.Column(db.Text)
    concept = db.Column(db.Text)
    hook = db.Column(db.Text)
    script_outline = db.Column(db.Text)
    visual_notes = db.Column(db.Text)
    cta = db.Column(db.Text)
    status = db.Column(db.String(20), default='Drafting')  # Drafting, Pitching, Assigned, Complete
    # Posted video metrics (legacy, per-campaign)
    posted_url = db.Column(db.String)
    posted_at = db.Column(db.DateTime)
    views = db.Column(db.Integer)
    likes = db.Column(db.Integer)
    comments = db.Column(db.Integer)
    shares = db.Column(db.Integer)
    saves = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def total_views(self):
        return sum(cv.views or 0 for cv in self.campaign_videos)

    @property
    def total_likes(self):
        return sum(cv.likes or 0 for cv in self.campaign_videos)

    @property
    def total_comments(self):
        return sum(cv.comments or 0 for cv in self.campaign_videos)

    @property
    def total_shares(self):
        return sum(cv.shares or 0 for cv in self.campaign_videos)

    @property
    def total_saves(self):
        return sum(cv.saves or 0 for cv in self.campaign_videos)

    @property
    def display_name(self):
        if self.name:
            return self.name
        if self.product:
            return f'{self.product.name} Campaign'
        return f'Campaign #{self.id}'


DEFAULT_FRAMEWORKS = [
    {
        "name": "Trend Hijack",
        "description": "Rides a viral audio, meme format, or cultural moment and applies it to your message.",
        "structure": "1. Use the trending audio/format faithfully\n2. Subvert or apply it to your niche\n3. Punchline or payoff tied to the trend",
        "example_hooks": "Varies by trend — the hook IS the trend format."
    },
]

# Names of the original default frameworks to remove on next startup
_STALE_DEFAULT_FRAMEWORKS = [
    "Hook + Value Bomb", "Before & After", "Story → Lesson",
    "Tutorial / How-To", "Social Proof", "Problem / Agitate / Solve",
    "Day in the Life",
]

DEFAULT_PRODUCTS = [
    {"name": "Readwise"},
    {"name": "Reader"},
    {"name": "Bookwise"},
]
