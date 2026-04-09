import json
import os
import anthropic

client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
MODEL = 'claude-opus-4-6'


def classify_video(video, frameworks, frames=None, transcript=None):
    """Match a video to one of the given frameworks. Returns (framework_id, analysis_text)."""
    if not frameworks:
        return None, None

    framework_list = '\n'.join(
        f"ID {f.id}: {f.name} — {f.description}" for f in frameworks
    )

    text_prompt = f"""You are a short-form video strategist. Analyse the video below and classify it into the best-matching framework from the list.

FRAMEWORKS:
{framework_list}

VIDEO:
- URL: {video.url}
- Platform: {video.platform}
- Creator: {video.creator or 'Unknown'}
- Title: {video.title or ''}
- Caption: {video.caption or ''}"""

    if transcript:
        text_prompt += f"\n- Transcript: {transcript}"

    text_prompt += """

Respond with JSON only, no markdown fences:
{
  "framework_id": <integer ID from the list above>,
  "analysis": "<2-3 sentences explaining why this framework applies and what makes this video effective>"
}"""

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
            max_tokens=512,
            messages=[{'role': 'user', 'content': content}]
        )
        raw = message.content[0].text.strip()
        data = json.loads(raw)
        return data.get('framework_id'), data.get('analysis')
    except Exception as e:
        return None, f'Classification error: {e}'


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
