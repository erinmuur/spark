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


def fetch_metadata(url):
    """Fetch video metadata via yt-dlp without downloading. Returns a dict."""
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
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
                'raw': json.dumps({
                    'title': info.get('title'),
                    'uploader': info.get('uploader'),
                    'description': info.get('description'),
                    'duration': info.get('duration'),
                    'view_count': info.get('view_count'),
                    'like_count': info.get('like_count'),
                    'tags': info.get('tags'),
                    'webpage_url': info.get('webpage_url'),
                }, default=str)
            }
    except Exception as e:
        return {'error': str(e)}


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
