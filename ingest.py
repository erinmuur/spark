import re
import json
import yt_dlp


VIDEO_URL_PATTERNS = [
    r'https?://(?:www\.)?tiktok\.com/@[^/]+/video/\d+[^\s>]*',
    r'https?://vm\.tiktok\.com/[^\s>]+',
    r'https?://vt\.tiktok\.com/[^\s>]+',
    r'https?://(?:www\.)?instagram\.com/(?:reel|p)/[^\s>/]+[^\s>]*',
    r'https?://(?:www\.)?twitter\.com/\w+/status/\d+[^\s>]*',
    r'https?://(?:www\.)?x\.com/\w+/status/\d+[^\s>]*',
]


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
    # Unescape Slack URL format
    unescaped = re.sub(r'<(https?://[^|>]+)(?:\|[^>]*)?>',  r'\1', text)

    urls = []
    for pattern in VIDEO_URL_PATTERNS:
        matches = re.findall(pattern, unescaped, re.IGNORECASE)
        urls.extend(matches)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def fetch_metadata(url):
    """Fetch video metadata via yt-dlp. Returns a dict."""
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

            # Pick the best thumbnail
            thumbnail = info.get('thumbnail', '')
            thumbnails = info.get('thumbnails', [])
            if thumbnails:
                # Prefer the highest resolution
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
