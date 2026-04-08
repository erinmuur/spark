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
    raw_metadata = db.Column(db.Text)      # JSON from yt-dlp
    slack_user = db.Column(db.String(100))
    slack_channel = db.Column(db.String(100))
    slack_ts = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    campaigns = db.relationship('Campaign', backref='video', lazy=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)  # Readwise, Reader, Bookwise
    description = db.Column(db.Text)
    key_benefits = db.Column(db.Text)
    target_audience = db.Column(db.Text)
    usp = db.Column(db.Text)
    voice_notes = db.Column(db.Text)

    campaigns = db.relationship('Campaign', backref='product', lazy=True)


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    context_notes = db.Column(db.Text)
    concept = db.Column(db.Text)
    hook = db.Column(db.Text)
    script_outline = db.Column(db.Text)
    visual_notes = db.Column(db.Text)
    cta = db.Column(db.Text)
    status = db.Column(db.String(20), default='draft')  # draft, final
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
        "name": "Story → Lesson",
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
        "example_hooks": "Varies by trend — the hook IS the trend format."
    },
    {
        "name": "Social Proof",
        "description": "Testimonial or reaction-driven content that builds trust through others' results.",
        "structure": "1. Tease the result or transformation\n2. Show/quote the social proof (review, DM, comment)\n3. Validate with your own take\n4. CTA",
        "example_hooks": "\"Someone DM'd me asking how they read 50 books last year. Here's what I told them.\"\n\"This review made my day — and it's exactly why we built Reader.\""
    },
    {
        "name": "Problem / Agitate / Solve",
        "description": "Classic PAS structure — name the problem, twist the knife, then offer the solution.",
        "structure": "1. Name a specific problem your audience has\n2. Agitate — make them feel the pain more acutely\n3. Introduce the solution\n4. CTA",
        "example_hooks": "\"You're reading every day but retaining nothing.\"\n\"Your highlights are just graveyard for good ideas.\""
    },
    {
        "name": "Day in the Life",
        "description": "Behind-the-scenes or routine content that builds parasocial connection and shows the product in context.",
        "structure": "1. Establish the routine/context\n2. Show the product in natural use\n3. Highlight a specific moment or result\n4. Soft CTA",
        "example_hooks": "\"My morning reading routine (and the app that makes it work).\"\n\"A week of reading — what I finished, what I highlighted, what stuck.\""
    },
]

DEFAULT_PRODUCTS = [
    {"name": "Readwise"},
    {"name": "Reader"},
    {"name": "Bookwise"},
]
