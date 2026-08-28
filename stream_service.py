from __future__ import annotations

import os
import re
from typing import Any

import requests
import yt_dlp

from ttl_cache import TtlLruCache

STREAM_URL_CACHE_MAX = int(os.getenv('STREAM_URL_CACHE_MAX', '96'))
STREAM_URL_CACHE_TTL = float(os.getenv('STREAM_URL_CACHE_TTL', '600'))

# googlevideo rejects open-ended Range (bytes=N-); cap each upstream request to 1 MiB.
_UPSTREAM_RANGE_CHUNK = 1024 * 1024

# Some player clients return DASH init URLs that only serve ~1 MiB (playback stops after ~1s).
_PLAYER_CLIENT_STRATEGIES = (
    ['web', '-android_sdkless'],
    ['web_safari', '-android_sdkless'],
    ['default', '-android_sdkless'],
)

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


def _is_progressive_audio(fmt: dict[str, Any]) -> bool:
    if not fmt.get('url'):
        return False
    if fmt.get('fragments'):
        return False
    protocol = fmt.get('protocol') or 'https'
    if protocol not in ('https', 'http'):
        return False
    if fmt.get('acodec') in (None, 'none'):
        return False
    if fmt.get('vcodec') not in (None, 'none'):
        return False
    container = (fmt.get('container') or fmt.get('ext') or '').lower()
    return 'dash' not in container


def _pick_audio_format(info: dict[str, Any]) -> dict[str, Any] | None:
    formats = info.get('formats') or []
    audio_only = [f for f in formats if _is_progressive_audio(f)]
    if not audio_only:
        return None

    def score(fmt: dict[str, Any]) -> tuple:
        itag = str(fmt.get('format_id') or '')
        ext = (fmt.get('ext') or '').lower()
        abr = fmt.get('abr') or 0
        prefer = 2 if itag == '140' else (1 if ext == 'm4a' else 0)
        return (prefer, abr)

    audio_only.sort(key=score, reverse=True)
    fmt = audio_only[0]
    return {
        'url': fmt['url'],
        'http_headers': fmt.get('http_headers') or info.get('http_headers') or {},
    }


def _extract_info(video_id: str, player_clients: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        'format': '140/bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]/best',
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': player_clients}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)


def _stream_target_from_info(info: dict[str, Any]) -> dict[str, str] | None:
    picked = _pick_audio_format(info or {})
    if not picked or not picked.get('url'):
        return None
    headers = {str(k): str(v) for k, v in (picked.get('http_headers') or {}).items()}
    return {'url': picked['url'], 'headers': headers}


def _probe_range(url: str, headers: dict[str, str], range_header: str) -> bool:
    probe_headers = dict(headers)
    probe_headers['Range'] = range_header
    try:
        upstream = requests.get(url, headers=probe_headers, stream=True, timeout=8)
    except requests.RequestException:
        return False
    ok = upstream.status_code == 206
    upstream.close()
    return ok


def _url_is_fully_proxyable(url: str, headers: dict[str, str]) -> bool:
    # Reject DASH init URLs that only serve the first megabyte.
    if not _probe_range(url, headers, 'bytes=0-1023'):
        return False
    return _probe_range(url, headers, 'bytes=1048576-1048576')


def _extract_stream_target(video_id: str) -> dict[str, str]:
    last_error: Exception | None = None
    for player_clients in _PLAYER_CLIENT_STRATEGIES:
        try:
            info = _extract_info(video_id, player_clients)
        except Exception as exc:
            last_error = exc
            continue

        target = _stream_target_from_info(info)
        if not target:
            continue
        if _url_is_fully_proxyable(target['url'], target['headers']):
            return target

    if last_error is not None:
        raise _classify_ytdlp_error(last_error) from last_error
    raise StreamResolveError('unavailable', 'No proxyable audio stream found', 404)


def _normalize_upstream_range(range_header: str | None) -> str:
    """Map open-ended browser Range values to bounded chunks googlevideo accepts."""
    if not range_header:
        end = _UPSTREAM_RANGE_CHUNK - 1
        return f'bytes=0-{end}'

    value = range_header.strip()
    if re.fullmatch(r'bytes=0-', value, re.I):
        end = _UPSTREAM_RANGE_CHUNK - 1
        return f'bytes=0-{end}'

    open_ended = re.fullmatch(r'bytes=(\d+)-', value, re.I)
    if open_ended:
        start = int(open_ended.group(1))
        end = start + _UPSTREAM_RANGE_CHUNK - 1
        return f'bytes={start}-{end}'

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
        if cached and _url_is_fully_proxyable(cached['url'], cached['headers']):
            return cached
        if cached:
            _url_cache.invalidate(video_id)

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
