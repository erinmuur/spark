import re
import os
import json
import glob
import base64
import subprocess
import tempfile
import yt_dlp


VIDEO_URL_PATTERNS = [
    r'https?://(?:www\.)?tiktok\.com/@[^/]+/video/\d+[^\s>]*',
    r'https?://vm\.tiktok\.com/[^\s>]+',
    r'https?://vt\.tiktok\.com/[^\s>]+',
    r'https?://(?:www\.)?instagram\.com/(?:reel|p)/[^\s>/]+[^\s>]*',
    r'https?://(?:www\.)?twitter\.com/\w+/status/\d+[^\s>]*',
    r'https?://(?:www\.)?x\.com/\w+/status/\d+[^\s>]*',
]

_whisper_model = None


def _load_whisper():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model('tiny')
    return _whisper_model


def detect_platform(url):
    if 'tiktok.com' in url:
        return 'tiktok'
    elif 'instagram.com' in url:
        return 'instagram'
    elif 'twitter.com' in url or 'x.com' in url:
        return 'twitter'
    return 'unknown'


def extract_video_urls(text):
    """Extract video URLs from Slack message text.
    Slack wraps URLs like <https://...> or <https://...|display text>.
    """
    unescaped = re.sub(r'<(https?://[^|>]+)(?:\|[^>]*)?>',  r'\1', text)

    urls = []
    for pattern in VIDEO_URL_PATTERNS:
        matches = re.findall(pattern, unescaped, re.IGNORECASE)
        urls.extend(matches)

    seen = set()
    result = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _fetch_via_subprocess(url):
    """Use yt-dlp CLI in a subprocess — avoids in-process TikTok extraction bugs."""
    result = subprocess.run(
        ['yt-dlp', '--dump-json', '--skip-download', '--no-warnings',
         '--extractor-args', 'tiktok:api_hostname=api22-normal-c-alisg.tiktokv.com',
         url],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0 and result.stdout.strip():
        return json.loads(result.stdout)
    return None


def _clean_url(url):
    """Strip tracking query params from TikTok/Instagram URLs that break extractors."""
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if 'tiktok.com' in parsed.netloc or 'instagram.com' in parsed.netloc:
        return urlunparse(parsed._replace(query='', fragment=''))
    return url


def fetch_metadata(url):
    """Fetch video metadata via yt-dlp without downloading. Returns a dict."""
    import time as _time
    url = _clean_url(url)

    is_tiktok = 'tiktok.com' in url
    max_attempts = 3 if is_tiktok else 1
    last_err = None
    info = None

    for attempt in range(max_attempts):
        if attempt > 0:
            _time.sleep(attempt * 4)  # 4s, 8s backoff
        try:
            # Use subprocess for TikTok to avoid in-process extraction issues
            if is_tiktok:
                info = _fetch_via_subprocess(url)
            else:
                ydl_opts = {
                    'skip_download': True,
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': False,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)

            if info is not None:
                break
            last_err = 'no info returned'
        except Exception as e:
            last_err = str(e)
            continue
    else:
        # All attempts failed — try Instagram fallback or return error
        if 'instagram.com' in url:
            fallback = _scrape_instagram_meta(url)
            if fallback:
                return fallback
        return {'error': last_err or 'fetch failed'}

    if info is None:
        return None

    thumbnail = info.get('thumbnail', '')
    thumbnails = info.get('thumbnails', [])
    if thumbnails:
        best = max(thumbnails, key=lambda t: (t.get('width', 0) or 0) * (t.get('height', 0) or 0), default=None)
        if best:
            thumbnail = best.get('url', thumbnail)

    creator = (
        info.get('uploader')
        or info.get('creator')
        or info.get('channel')
        or ''
    )

    return {
        'title': info.get('title', ''),
        'creator': creator,
        'caption': info.get('description', ''),
        'thumbnail_url': thumbnail,
        'duration': info.get('duration', 0),
        'platform': detect_platform(url),
        'view_count': info.get('view_count'),
        'like_count': info.get('like_count'),
        'comment_count': info.get('comment_count'),
        'share_count': info.get('repost_count') or info.get('share_count'),
        'save_count': info.get('digg_count') or info.get('favorite_count'),
        'raw': json.dumps({
            'title': info.get('title'),
            'uploader': info.get('uploader'),
            'description': info.get('description'),
            'duration': info.get('duration'),
            'view_count': info.get('view_count'),
            'like_count': info.get('like_count'),
            'comment_count': info.get('comment_count'),
            'repost_count': info.get('repost_count'),
            'digg_count': info.get('digg_count'),
            'upload_date': info.get('upload_date'),
            'timestamp': info.get('timestamp'),
            'tags': info.get('tags'),
            'webpage_url': info.get('webpage_url'),
        }, default=str)
    }


def _scrape_instagram_meta(url):
    """Fallback: scrape Instagram post page for thumbnail and basic info."""
    import requests
    try:
        r = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        })
        if r.status_code != 200:
            return None

        # Extract CDN thumbnail
        images = re.findall(r'(https://scontent[^"\'>\s]+)', r.text)
        thumbnail = images[0].replace('&amp;', '&') if images else ''

        # Extract title from <title> tag
        title_match = re.search(r'<title>([^<]+)</title>', r.text)
        title = title_match.group(1).strip() if title_match else ''

        # Try to extract username from title like "Post by username"
        creator = ''
        creator_match = re.search(r'(?:Post by|@)\s*(\w[\w.]+)', title)
        if creator_match:
            creator = creator_match.group(1)

        # Build embed HTML
        shortcode_match = re.search(r'/p/([^/]+)', url)
        shortcode = shortcode_match.group(1) if shortcode_match else ''
        embed_html = (
            f'<blockquote class="instagram-media" '
            f'data-instgrm-permalink="https://www.instagram.com/p/{shortcode}/" '
            f'data-instgrm-version="14" style="max-width:540px;width:100%;"></blockquote>'
        ) if shortcode else ''

        return {
            'title': title,
            'creator': creator,
            'caption': '',
            'thumbnail_url': thumbnail,
            'duration': 0,
            'platform': 'instagram',
            'view_count': None,
            'like_count': None,
            'comment_count': None,
            'share_count': None,
            'save_count': None,
            'embed_html': embed_html,
            'raw': json.dumps({'title': title, 'uploader': creator, 'type': 'image_post'}),
        }
    except Exception:
        return None


def fetch_oembed(url, platform):
    """Fetch oEmbed HTML from the platform. Returns HTML string or None."""
    import requests
    if platform == 'twitter':
        # Normalize to twitter.com — X's oEmbed endpoint requires it
        oembed_url = 'https://publish.twitter.com/oembed?url=' + url.replace('x.com', 'twitter.com') + '&dnt=true'
        try:
            r = requests.get(oembed_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0 (compatible)'})
            if r.status_code == 200:
                return r.json().get('html', '')
        except Exception:
            pass
        return None

    elif platform == 'tiktok':
        # Try oEmbed API first
        try:
            r = requests.get(
                'https://www.tiktok.com/oembed?url=' + url,
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0 (compatible)'}
            )
            if r.status_code == 200:
                html = r.json().get('html', '')
                if html:
                    return html
        except Exception:
            pass
        # Fallback: build blockquote embed directly from video ID in URL
        m = re.search(r'/video/(\d+)', url)
        if m:
            video_id = m.group(1)
            return (
                f'<blockquote class="tiktok-embed" cite="{url}" data-video-id="{video_id}" '
                f'style="width:100%;max-width:100%;"><section></section></blockquote>'
                f'<script async src="https://www.tiktok.com/embed.js"></script>'
            )

    return None


def fetch_rich_content(url, duration=None):
    """Download video, extract frames and transcribe audio.
    Returns dict with 'frames' (list of base64 JPEGs) and 'transcript' (str).
    """
    result = {'frames': [], 'transcript': ''}

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, 'video.%(ext)s')
        ydl_opts = {
            'outtmpl': output_template,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            result['error'] = str(e)
            return result

        # Find downloaded file
        files = [f for f in glob.glob(os.path.join(tmpdir, 'video.*'))
                 if f.split('.')[-1] in ('mp4', 'webm', 'mkv', 'mov', 'm4v')]
        if not files:
            return result
        video_path = files[0]

        # Extract evenly-spaced frames
        num_frames = 6
        if duration and duration > 0:
            interval = duration / (num_frames + 1)
            timestamps = [interval * (i + 1) for i in range(num_frames)]
        else:
            timestamps = [2, 5, 8, 12, 16, 20]

        for i, ts in enumerate(timestamps):
            frame_path = os.path.join(tmpdir, f'frame_{i}.jpg')
            cmd = [
                'ffmpeg', '-ss', str(ts), '-i', video_path,
                '-vframes', '1', '-q:v', '3',
                '-vf', 'scale=768:-1',
                frame_path, '-y', '-loglevel', 'error'
            ]
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode == 0 and os.path.exists(frame_path):
                with open(frame_path, 'rb') as f:
                    result['frames'].append(base64.b64encode(f.read()).decode('utf-8'))

        # Transcribe audio with Whisper
        try:
            model = _load_whisper()
            whisper_result = model.transcribe(video_path, fp16=False)
            result['transcript'] = whisper_result['text'].strip()
        except Exception as e:
            result['transcript'] = ''

    return result
