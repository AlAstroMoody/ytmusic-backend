from __future__ import annotations

import os
from typing import Any

import requests
import yt_dlp

from ttl_cache import TtlLruCache

STREAM_URL_CACHE_MAX = int(os.getenv('STREAM_URL_CACHE_MAX', '96'))
STREAM_URL_CACHE_TTL = float(os.getenv('STREAM_URL_CACHE_TTL', '600'))

# Cached stream target: direct googlevideo URL + headers yt-dlp expects for that format.
_url_cache: TtlLruCache[str, dict[str, str]] = TtlLruCache(STREAM_URL_CACHE_MAX, STREAM_URL_CACHE_TTL)


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


def _ydl_opts() -> dict[str, Any]:
    return {
        'format': '140/bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {'player_client': ['default', '-android_sdkless']},
        },
    }


def _pick_audio_format(info: dict[str, Any]) -> dict[str, Any] | None:
    formats = info.get('formats') or []
    audio_only = [
        f for f in formats
        if f.get('url') and f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')
    ]
    if not audio_only:
        url = info.get('url')
        if not url:
            return None
        return {'url': url, 'http_headers': info.get('http_headers') or {}}

    def score(fmt: dict[str, Any]) -> tuple:
        itag = str(fmt.get('format_id') or '')
        ext = (fmt.get('ext') or '').lower()
        abr = fmt.get('abr') or 0
        prefer = 2 if itag == '140' or ext == 'm4a' else (1 if ext in ('webm', 'opus') else 0)
        dash_penalty = 1 if fmt.get('fragments') or 'dash' in (fmt.get('format') or '').lower() else 0
        return (prefer, -dash_penalty, abr)

    audio_only.sort(key=score, reverse=True)
    fmt = audio_only[0]
    return {
        'url': fmt['url'],
        'http_headers': fmt.get('http_headers') or info.get('http_headers') or {},
    }


def _extract_stream_target(video_id: str) -> dict[str, str]:
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
    except Exception as exc:
        raise _classify_ytdlp_error(exc) from exc

    picked = _pick_audio_format(info or {})
    if not picked or not picked.get('url'):
        raise StreamResolveError('unavailable', 'No audio stream found', 404)

    headers = {str(k): str(v) for k, v in (picked.get('http_headers') or {}).items()}
    return {'url': picked['url'], 'headers': headers}


def _normalize_upstream_range(range_header: str | None) -> str:
    """googlevideo rejects open-ended and missing Range on current DASH/fmp4 URLs."""
    if not range_header:
        return 'bytes=0-65535'
    value = range_header.strip()
    if value.lower() in ('bytes=0-', 'bytes=0'):
        return 'bytes=0-65535'
    return value


def _upstream_headers(
    stream_headers: dict[str, str],
    range_header: str | None,
) -> dict[str, str]:
    headers = dict(stream_headers)
    headers['Range'] = _normalize_upstream_range(range_header)
    return headers


def resolve_stream_target(video_id: str, *, bypass_cache: bool = False) -> dict[str, str]:
    if not bypass_cache:
        cached = _url_cache.get(video_id)
        if cached:
            return cached

    target = _extract_stream_target(video_id)
    _url_cache.set(video_id, target)
    return target


def resolve_audio_url(video_id: str, *, bypass_cache: bool = False) -> str:
    return resolve_stream_target(video_id, bypass_cache=bypass_cache)['url']


def invalidate_audio_url(video_id: str) -> None:
    _url_cache.invalidate(video_id)


def open_audio_upstream(
    video_id: str,
    range_header: str | None = None,
) -> tuple[requests.Response, str]:
    """Open googlevideo stream; retry once with fresh URL on 403/410."""
    target = resolve_stream_target(video_id)
    url = target['url']
    headers = _upstream_headers(target['headers'], range_header)

    upstream = requests.get(url, headers=headers, stream=True, timeout=30)

    if upstream.status_code in (403, 410):
        upstream.close()
        invalidate_audio_url(video_id)
        try:
            target = resolve_stream_target(video_id, bypass_cache=True)
        except StreamResolveError:
            raise StreamResolveError('expired', 'Stream URL expired and refresh failed', 410)
        url = target['url']
        headers = _upstream_headers(target['headers'], range_header)
        upstream = requests.get(url, headers=headers, stream=True, timeout=30)
        if upstream.status_code in (403, 410):
            upstream.close()
            invalidate_audio_url(video_id)
            raise StreamResolveError('expired', 'Stream URL expired', 410)

    return upstream, url
