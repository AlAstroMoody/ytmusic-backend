from __future__ import annotations

import os
from typing import Any

import requests
import yt_dlp

from ttl_cache import TtlLruCache

STREAM_URL_CACHE_MAX = int(os.getenv('STREAM_URL_CACHE_MAX', '96'))
STREAM_URL_CACHE_TTL = float(os.getenv('STREAM_URL_CACHE_TTL', '600'))

_url_cache: TtlLruCache[str, str] = TtlLruCache(STREAM_URL_CACHE_MAX, STREAM_URL_CACHE_TTL)


class StreamResolveError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _classify_ytdlp_error(exc: Exception) -> StreamResolveError:
    text = str(exc).lower()
    if 'private' in text or 'login' in text:
        return StreamResolveError('unavailable', str(exc), 404)
    if 'not available' in text or 'unavailable' in text or 'removed' in text:
        return StreamResolveError('unavailable', str(exc), 404)
    if 'geo' in text or 'country' in text or 'not made this video available' in text:
        return StreamResolveError('geo', str(exc), 451)
    return StreamResolveError('upstream', str(exc), 502)


def _pick_audio_url(info: dict[str, Any]) -> str | None:
    formats = info.get('formats') or []
    audio_only = [
        f for f in formats
        if f.get('url') and f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')
    ]
    if not audio_only:
        return info.get('url')

    def score(fmt: dict[str, Any]) -> tuple:
        itag = str(fmt.get('format_id') or '')
        ext = (fmt.get('ext') or '').lower()
        abr = fmt.get('abr') or 0
        # prefer m4a / itag 140, then higher bitrate
        prefer = 2 if itag == '140' or ext == 'm4a' else (1 if ext in ('webm', 'opus') else 0)
        return (prefer, abr)

    audio_only.sort(key=score, reverse=True)
    return audio_only[0].get('url')


def resolve_audio_url(video_id: str, *, bypass_cache: bool = False) -> str:
    if not bypass_cache:
        cached = _url_cache.get(video_id)
        if cached:
            return cached

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
    except Exception as exc:
        raise _classify_ytdlp_error(exc) from exc

    url = _pick_audio_url(info or {})
    if not url:
        raise StreamResolveError('unavailable', 'No audio stream found', 404)

    _url_cache.set(video_id, url)
    return url


def invalidate_audio_url(video_id: str) -> None:
    _url_cache.invalidate(video_id)


def open_audio_upstream(
    video_id: str,
    range_header: str | None = None,
) -> tuple[requests.Response, str]:
    """Open googlevideo stream; retry once with fresh URL on 403/410."""
    headers: dict[str, str] = {}
    if range_header:
        headers['Range'] = range_header

    url = resolve_audio_url(video_id)
    upstream = requests.get(url, headers=headers, stream=True, timeout=30)

    if upstream.status_code in (403, 410):
        upstream.close()
        invalidate_audio_url(video_id)
        try:
            url = resolve_audio_url(video_id, bypass_cache=True)
        except StreamResolveError:
            raise StreamResolveError('expired', 'Stream URL expired and refresh failed', 410)
        upstream = requests.get(url, headers=headers, stream=True, timeout=30)
        if upstream.status_code in (403, 410):
            upstream.close()
            invalidate_audio_url(video_id)
            raise StreamResolveError('expired', 'Stream URL expired', 410)

    return upstream, url
