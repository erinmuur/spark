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


PLATFORM_ORDER = ['tiktok', 'instagram', 'youtube', 'twitter', 'unknown']
PLATFORM_LABELS = {
    'tiktok': 'TikTok',
    'instagram': 'Instagram',
    'youtube': 'YouTube',
    'twitter': 'X',
    'unknown': 'Other',
}
PLATFORM_ABBR = {
    'tiktok': 'TT',
    'instagram': 'IG',
    'youtube': 'YT',
    'twitter': 'X',
    'unknown': '?',
}


def platform_abbr(platform):
    return PLATFORM_ABBR.get(platform, (platform or '?')[:2].upper())


class Video(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String, nullable=False, unique=True)
    platform = db.Column(db.String(50))   # tiktok, instagram, youtube, twitter
    creator = db.Column(db.String(200))
    title = db.Column(db.String(500))
    caption = db.Column(db.Text)
    thumbnail_url = db.Column(db.String)
    framework_id = db.Column(db.Integer, db.ForeignKey('framework.id'))
    analysis = db.Column(db.Text)          # Claude's framework analysis
    transcript = db.Column(db.Text)        # Whisper speech-to-text
    embed_html = db.Column(db.Text)        # oEmbed HTML from platform
    raw_metadata = db.Column(db.Text)      # JSON from yt-dlp
    follower_count = db.Column(db.Integer)   # creator's follower count at time of scrape
    slack_user = db.Column(db.String(200))   # display name resolved at ingest time
    slack_channel = db.Column(db.String(100))
    slack_ts = db.Column(db.String(50))
    slack_message = db.Column(db.Text)       # message text with URL stripped out
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    favorited = db.Column(db.Boolean, default=False)
    video_format = db.Column(db.String(50))  # POV, Talking Head, etc.
    tribe_scores = db.Column(db.Text)        # JSON list of per-second mean brain activations (TRIBE v2)
    tribe_suggestions = db.Column(db.Text)   # Claude's editing suggestions from TRIBE analysis
    tribe_status = db.Column(db.String(50))  # idle / running / done / error:...
    comments_json = db.Column(db.Text)       # JSON array of top comments [{text, likes, username}, ...]

    # Canonical per-upload metrics. A view count belongs to the upload, not to
    # whichever campaign happens to reference it, so these live here and get
    # mirrored onto CampaignVideo rows for the legacy campaign totals.
    views = db.Column(db.Integer)
    likes = db.Column(db.Integer)
    comments = db.Column(db.Integer)
    shares = db.Column(db.Integer)
    saves = db.Column(db.Integer)
    metrics_updated_at = db.Column(db.DateTime)

    post_id = db.Column(db.Integer, db.ForeignKey('post.id'))

    campaigns = db.relationship('Campaign', backref='video', lazy=True, cascade='all, delete-orphan')

    @property
    def platform_label(self):
        return PLATFORM_LABELS.get(self.platform, (self.platform or 'Other').title())

    @property
    def engagements(self):
        """Likes + comments + shares + saves — the interaction total."""
        return sum(v or 0 for v in (self.likes, self.comments, self.shares, self.saves))

    @property
    def engagement_rate(self):
        """Engagements as a percentage of views, or None if views are unknown."""
        if not self.views:
            return None
        return self.engagements / self.views * 100

    @property
    def has_metrics(self):
        return any(v is not None for v in
                   (self.views, self.likes, self.comments, self.shares, self.saves))


class Post(db.Model):
    """One piece of UGC creative, published to one or more platforms.

    Creators typically upload the same video to TikTok, Instagram and (less
    often) YouTube. Each upload is its own Video row; the Post groups them so
    performance can be read as one creative with a per-platform breakdown.
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500))
    creator = db.Column(db.String(200))
    creator_key = db.Column(db.String(200))   # normalized handle, used for auto-grouping
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    notes = db.Column(db.Text)
    posted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    videos = db.relationship(
        'Video', backref=db.backref('post', lazy=True),
        lazy=True, order_by='Video.id',
    )
    campaign = db.relationship('Campaign', backref=db.backref('posts', lazy=True))

    @property
    def display_name(self):
        if self.title:
            return self.title
        for v in self.videos:
            if v.title:
                return v.title
        if self.creator:
            return f'@{self.creator}'
        return f'Post #{self.id}'

    @property
    def ordered_videos(self):
        """Videos in a stable platform order so columns don't shuffle between loads."""
        return sorted(
            self.videos,
            key=lambda v: (PLATFORM_ORDER.index(v.platform)
                           if v.platform in PLATFORM_ORDER else len(PLATFORM_ORDER), v.id)
        )

    @property
    def platforms(self):
        seen = []
        for v in self.ordered_videos:
            if v.platform and v.platform not in seen:
                seen.append(v.platform)
        return seen

    @property
    def missing_platforms(self):
        """Core platforms this creative hasn't been posted to yet."""
        return [p for p in ('tiktok', 'instagram', 'youtube') if p not in self.platforms]

    def _total(self, field):
        vals = [getattr(v, field) for v in self.videos]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    @property
    def total_views(self):
        return self._total('views')

    @property
    def total_likes(self):
        return self._total('likes')

    @property
    def total_comments(self):
        return self._total('comments')

    @property
    def total_shares(self):
        return self._total('shares')

    @property
    def total_saves(self):
        return self._total('saves')

    @property
    def total_engagements(self):
        return sum(v.engagements for v in self.videos)

    @property
    def engagement_rate(self):
        views = self.total_views
        if not views:
            return None
        return self.total_engagements / views * 100

    @property
    def top_video(self):
        """The platform upload with the most views."""
        scored = [v for v in self.videos if v.views]
        return max(scored, key=lambda v: v.views) if scored else None

    @property
    def thumbnail_video(self):
        """Preferred video for the card thumbnail — first one that has an image."""
        for v in self.ordered_videos:
            if v.thumbnail_url:
                return v
        return self.videos[0] if self.videos else None


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
    video = db.relationship('Video', backref=db.backref('campaign_video_links', lazy=True, cascade='all, delete-orphan'))

    __table_args__ = (db.UniqueConstraint('campaign_id', 'video_id', name='uq_campaign_video'),)

    def metric(self, field):
        """Campaign-local override if set, otherwise the video's canonical metric."""
        own = getattr(self, field)
        if own is not None:
            return own
        return getattr(self.video, field, None) if self.video else None


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

    def _total(self, field):
        return sum(cv.metric(field) or 0 for cv in self.campaign_videos)

    @property
    def total_views(self):
        return self._total('views')

    @property
    def total_likes(self):
        return self._total('likes')

    @property
    def total_comments(self):
        return self._total('comments')

    @property
    def total_shares(self):
        return self._total('shares')

    @property
    def total_saves(self):
        return self._total('saves')

    @property
    def grouped_posts(self):
        """UGC posts for this campaign, best-performing first."""
        return sorted(self.posts, key=lambda p: p.total_views or 0, reverse=True)

    @property
    def loose_videos(self):
        """Campaign videos not yet grouped into a UGC post."""
        return [cv.video for cv in self.campaign_videos if cv.video and not cv.video.post_id]

    @property
    def display_name(self):
        if self.name:
            return self.name
        if self.product:
            return f'{self.product.name} Campaign'
        return f'Campaign #{self.id}'


DEFAULT_FRAMEWORKS = [
    {
        "name": "Hook + Value Bomb",
        "description": "Opens with a bold, attention-grabbing claim immediately followed by rapid, dense value delivery.",
        "structure": "1. Shocking or counterintuitive hook (0-3s)\n2. Rapid-fire payoff — deliver the goods immediately\n3. Brief recap or CTA",
        "example_hooks": "\"I read 100 books in a year. Here's the only system that worked.\"\n\"Stop highlighting. Do this instead.\""
    },
    {
        "name": "Before & After",
        "description": "Transformation arc that shows contrast between a problem state and a resolved state.",
        "structure": "1. Show the 'before' — pain, struggle, or mediocrity\n2. Introduce the turning point\n3. Reveal the 'after' — transformation or result\n4. CTA",
        "example_hooks": "\"This was my reading list in January. This is it now.\"\n\"I used to forget everything I read. Not anymore.\""
    },
    {
        "name": "Story \u2192 Lesson",
        "description": "Personal narrative arc with an explicit takeaway that the viewer can apply.",
        "structure": "1. Set the scene (who, when, what happened)\n2. Conflict or challenge\n3. Resolution\n4. Explicit lesson or takeaway\n5. CTA",
        "example_hooks": "\"Three years ago I quit reading entirely. Here's what changed.\"\n\"My boss asked me to summarize a book in 5 minutes. This is what happened.\""
    },
    {
        "name": "Tutorial / How-To",
        "description": "Step-by-step demonstration of how to do something specific and actionable.",
        "structure": "1. State what you're teaching\n2. Brief credibility signal\n3. Step-by-step walkthrough\n4. Result/payoff\n5. CTA",
        "example_hooks": "\"Here's exactly how I process a book in 20 minutes.\"\n\"3 steps to actually remember what you read.\""
    },
    {
        "name": "Trend Hijack",
        "description": "Rides a viral audio, meme format, or cultural moment and applies it to your message.",
        "structure": "1. Use the trending audio/format faithfully\n2. Subvert or apply it to your niche\n3. Punchline or payoff tied to the trend",
        "example_hooks": "Varies by trend \u2014 the hook IS the trend format."
    },
    {
        "name": "Social Proof",
        "description": "Testimonial or reaction-driven content that builds trust through others' results.",
        "structure": "1. Tease the result or transformation\n2. Show/quote the social proof (review, DM, comment)\n3. Validate with your own take\n4. CTA",
        "example_hooks": "\"Someone DM'd me asking how they read 50 books last year. Here's what I told them.\"\n\"This review made my day \u2014 and it's exactly why we built Reader.\""
    },
    {
        "name": "Problem / Agitate / Solve",
        "description": "Classic PAS structure \u2014 name the problem, twist the knife, then offer the solution.",
        "structure": "1. Name a specific problem your audience has\n2. Agitate \u2014 make them feel the pain more acutely\n3. Introduce the solution\n4. CTA",
        "example_hooks": "\"You're reading every day but retaining nothing.\"\n\"Your highlights are just graveyard for good ideas.\""
    },
    {
        "name": "Day in the Life",
        "description": "Behind-the-scenes or routine content that builds parasocial connection and shows the product in context.",
        "structure": "1. Establish the routine/context\n2. Show the product in natural use\n3. Highlight a specific moment or result\n4. Soft CTA",
        "example_hooks": "\"My morning reading routine (and the app that makes it work).\"\n\"A week of reading \u2014 what I finished, what I highlighted, what stuck.\""
    },
]

_STALE_DEFAULT_FRAMEWORKS = []

DEFAULT_PRODUCTS = [
    {"name": "Readwise"},
    {"name": "Reader"},
    {"name": "Bookwise"},
]
