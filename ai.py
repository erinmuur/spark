import json
import os
import anthropic

client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
MODEL = 'claude-opus-4-6'

UGC_FORMATS = [
    'POV',
    'Talking Head',
    'Text on Screen',
    'Trending Audio',
    'Relatable',
    'Reaction',
    'Greenscreen',
    'Snapchat Text',
    'Skit',
]

_FORMAT_DEFINITIONS = """- POV: Shows "POV" text on screen or first-person handheld perspective footage
- Talking Head: Creator talking directly to camera, full or split screen
- Text on Screen: Text overlays are the primary storytelling mechanism
- Trending Audio: Short video using trending/popular audio, no original creator voiceover
- Relatable: Depicts a relatable scenario, feeling, or situation the audience recognises
- Reaction: Creator reacts to another video, content, or situation on screen
- Greenscreen: Creator overlaid on top of another clip as a background
- Snapchat Text: White text on a coloured background in Snapchat style
- Skit: Scripted short scene or comedy sketch"""


def classify_video(video, frameworks, frames=None, transcript=None):
    """Match a video to a framework AND detect its UGC format.
    Returns (framework_id, analysis_text, video_format)."""
    if not frameworks:
        return None, None, None

    framework_list = '\n'.join(
        f"ID {f.id}: {f.name} — {f.description}" for f in frameworks
    )

    text_prompt = f"""You are a short-form video strategist. Analyse the video below and:
1. Classify it into the best-matching framework from the list.
2. Identify its UGC format.

FRAMEWORKS:
{framework_list}

UGC FORMATS (pick exactly one):
{_FORMAT_DEFINITIONS}

VIDEO:
- URL: {video.url}
- Platform: {video.platform}
- Creator: {video.creator or 'Unknown'}
- Title: {video.title or ''}
- Caption: {video.caption or ''}"""

    if transcript:
        text_prompt += f"\n- Transcript: {transcript}"

    text_prompt += f"""

IMPORTANT INSTRUCTIONS FOR ANALYSIS:
- Describe what SPECIFICALLY happens in this video — the actual opening moment, the specific words or phrases used, the visual action taken. Do NOT describe what the framework means in general.
- If the available information is limited (no transcript, no frames), say what you can infer from the caption/title and flag your confidence.
- For UGC format: classify based on the actual content signals, not assumptions from the creator name or platform.

Respond with JSON only, no markdown fences:
{{
  "framework_id": <integer ID from the list above>,
  "analysis": "<2-3 sentences describing what specifically happens in THIS video and why it works — reference actual moments, words, or visuals from the content, not generic descriptions of the framework>",
  "video_format": "<one of: {', '.join(UGC_FORMATS)}>"
}}"""

    if frames:
        content = [{"type": "text", "text": "Here are frames from the video:"}]
        for frame in frames:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": frame}
            })
        content.append({"type": "text", "text": text_prompt})
    else:
        content = text_prompt

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=600,
            messages=[{'role': 'user', 'content': content}]
        )
        raw = message.content[0].text.strip()
        data = json.loads(raw)
        fmt = data.get('video_format')
        if fmt not in UGC_FORMATS:
            fmt = None
        return data.get('framework_id'), data.get('analysis'), fmt
    except Exception as e:
        return None, f'Classification error: {e}', None


def classify_format(video):
    """Classify the UGC format of an already-ingested video using text metadata only.
    Used to backfill existing videos that pre-date format detection."""
    prompt = f"""You are a short-form video analyst. Classify this video into exactly one UGC format.

FORMATS:
{_FORMAT_DEFINITIONS}

VIDEO:
- Platform: {video.platform}
- Creator: {video.creator or 'Unknown'}
- Caption: {video.caption or ''}
- Transcript: {video.transcript or ''}
- Framework analysis: {video.analysis or ''}

Respond with JSON only: {{"video_format": "<one of: {', '.join(UGC_FORMATS)}>"}}"""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=64,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = message.content[0].text.strip()
        fmt = json.loads(raw).get('video_format', '')
        return fmt if fmt in UGC_FORMATS else None
    except Exception:
        return None


def generate_campaign(video, framework, product, context_notes):
    """Generate a creative brief. Returns a dict with campaign fields."""
    framework_info = ''
    if framework:
        framework_info = f"""Framework: {framework.name}
Description: {framework.description}
Example hooks: {framework.example_hooks}"""

    product_info = f"""Product: {product.name}
Description: {product.description or '(not yet filled in)'}
Key benefits: {product.key_benefits or '(not yet filled in)'}
Target audience: {product.target_audience or '(not yet filled in)'}
Unique selling point: {product.usp or '(not yet filled in)'}
Tone / voice notes: {product.voice_notes or '(not yet filled in)'}"""

    video_info = f"""URL: {video.url}
Platform: {video.platform}
Creator: {video.creator or 'Unknown'}
Caption: {video.caption or ''}
Transcript: {video.transcript or ''}
Framework analysis: {video.analysis or ''}"""

    context = context_notes.strip() if context_notes else 'No additional context provided.'

    prompt = f"""You are a creative strategist for a software company. Generate a short-form video creative brief inspired by the video below, adapted for the given product.

INSPIRATION VIDEO:
{video_info}

VIDEO FRAMEWORK:
{framework_info}

PRODUCT PROFILE:
{product_info}

ADDITIONAL CONTEXT FROM TEAM:
{context}

Generate the brief as JSON only, no markdown fences:
{{
  "concept": "<2-3 sentences describing the video idea and how it adapts the framework for this product>",
  "hook": "<3 distinct opening line options, separated by newlines>",
  "script_outline": "<beat-by-beat script outline, 5-8 beats, each on a new line prefixed with a number>",
  "visual_notes": "<pacing, editing style, visual aesthetic, on-screen text suggestions>",
  "cta": "<how to close the video — what to say and what action to drive>"
}}"""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = message.content[0].text.strip()
        return json.loads(raw)
    except Exception as e:
        return {
            'concept': '',
            'hook': '',
            'script_outline': '',
            'visual_notes': '',
            'cta': '',
            'error': str(e)
        }
