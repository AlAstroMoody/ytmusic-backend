from __future__ import annotations

import os
import re
from typing import Any, Iterator

import requests
import yt_dlp

from ttl_cache import TtlLruCache

STREAM_URL_CACHE_MAX = int(os.getenv('STREAM_URL_CACHE_MAX', '96'))
STREAM_URL_CACHE_TTL = float(os.getenv('STREAM_URL_CACHE_TTL', '600'))

# googlevideo rejects open-ended Range (bytes=N-); cap each upstream request to 1 MiB.
_UPSTREAM_RANGE_CHUNK = 1024 * 1024

# DASH init URLs from some clients only serve the first megabyte; overlap past that limit.
_DASH_SINGLE_URL_LIMIT = 1024 * 1024
_OVERLAP = 128 * 1024

_PLAYER_CLIENT_STRATEGIES = (
    ['default', '-android_sdkless'],
    ['web_safari', '-android_sdkless'],
    ['web_creator', '-android_sdkless'],
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


def _pick_audio_format(info: dict[str, Any]) -> dict[str, str] | None:
    formats = info.get('formats') or []
    audio_only = [
        f for f in formats
        if f.get('url') and f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')
    ]
    if not audio_only:
        url = info.get('url')
        if not url:
            return None
        headers = {str(k): str(v) for k, v in (info.get('http_headers') or {}).items()}
        return {'url': url, 'headers': headers}

    def score(fmt: dict[str, Any]) -> tuple:
        itag = str(fmt.get('format_id') or '')
        ext = (fmt.get('ext') or '').lower()
        abr = fmt.get('abr') or 0
        prefer = 2 if itag == '140' or ext == 'm4a' else (1 if ext in ('webm', 'opus') else 0)
        return (prefer, abr)

    audio_only.sort(key=score, reverse=True)
    fmt = audio_only[0]
    headers = fmt.get('http_headers') or info.get('http_headers') or {}
    return {
        'url': fmt['url'],
        'headers': {str(k): str(v) for k, v in headers.items()},
    }


def _extract_info(video_id: str, player_clients: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': player_clients}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)


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
    if not _probe_range(url, headers, 'bytes=0-1023'):
        return False
    return _probe_range(url, headers, 'bytes=1048576-1048576')


def _extract_stream_target(video_id: str) -> dict[str, str]:
    last_error: Exception | None = None
    partial: dict[str, str] | None = None

    for player_clients in _PLAYER_CLIENT_STRATEGIES:
        try:
            info = _extract_info(video_id, player_clients)
        except Exception as exc:
            last_error = exc
            continue

        target = _pick_audio_format(info or {})
        if not target:
            continue
        if _url_is_fully_proxyable(target['url'], target['headers']):
            return target
        if partial is None and _probe_range(target['url'], target['headers'], 'bytes=0-1023'):
            partial = target

    if partial:
        return partial

    if last_error is not None:
        raise _classify_ytdlp_error(last_error) from last_error
    raise StreamResolveError('unavailable', 'No audio stream found', 404)


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


def _plan_upstream_fetch(range_header: str | None) -> tuple[str, int]:
    """Return upstream Range header and bytes to skip from the response body."""
    normalized = _normalize_upstream_range(range_header)
    matched = re.fullmatch(r'bytes=(\d+)-(\d+)', normalized, re.IGNORECASE)
    if not matched:
        return normalized, 0

    start = int(matched.group(1))
    end = int(matched.group(2))
    if start >= _DASH_SINGLE_URL_LIMIT:
        overlap_start = max(0, start - _OVERLAP)
        skip = start - overlap_start
        return f'bytes={overlap_start}-{end}', skip
    return normalized, 0


def _upstream_headers(
    stream_headers: dict[str, str],
    range_header: str | None,
) -> tuple[dict[str, str], int]:
    upstream_range, skip = _plan_upstream_fetch(range_header)
    headers = dict(stream_headers)
    headers['Range'] = upstream_range
    return headers, skip


def resolve_stream_target(video_id: str, *, bypass_cache: bool = False) -> dict[str, str]:
    if not bypass_cache:
        cached = _url_cache.get(video_id)
        if cached and _probe_range(cached['url'], cached['headers'], 'bytes=0-1023'):
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


def build_proxy_headers(upstream_headers: requests.structures.CaseInsensitiveDict, skip: int) -> dict[str, str]:
    allowed = ('content-type', 'content-length', 'content-range', 'accept-ranges')
    headers = {
        key: value
        for key, value in upstream_headers.items()
        if key.lower() in allowed
    }
    if skip <= 0:
        return headers

    content_range = headers.get('Content-Range') or headers.get('content-range')
    if content_range:
        matched = re.match(r'bytes (\d+)-(\d+)/(\d+|\*)', content_range)
        if matched:
            end, total = matched.group(2), matched.group(3)
            new_start = int(matched.group(1)) + skip
            headers['Content-Range'] = f'bytes {new_start}-{end}/{total}'

    content_length = headers.get('Content-Length') or headers.get('content-length')
    if content_length:
        headers['Content-Length'] = str(max(0, int(content_length) - skip))

    return headers


def iter_upstream_body(upstream: requests.Response, skip: int) -> Iterator[bytes]:
    skipped = 0
    for chunk in upstream.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        if skipped < skip:
            if skipped + len(chunk) <= skip:
                skipped += len(chunk)
                continue
            chunk = chunk[skip - skipped:]
            skipped = skip
        yield chunk


def open_audio_upstream(
    video_id: str,
    range_header: str | None = None,
) -> tuple[requests.Response, str, int]:
    """Open googlevideo stream; retry once with fresh URL on 403/410."""
    target = resolve_stream_target(video_id)
    url = target['url']
    headers, skip = _upstream_headers(target['headers'], range_header)

    upstream = requests.get(url, headers=headers, stream=True, timeout=30)

    if upstream.status_code in (403, 410):
        upstream.close()
        invalidate_audio_url(video_id)
        try:
            target = resolve_stream_target(video_id, bypass_cache=True)
        except StreamResolveError:
            raise StreamResolveError('expired', 'Stream URL expired and refresh failed', 410)
        url = target['url']
        headers, skip = _upstream_headers(target['headers'], range_header)
        upstream = requests.get(url, headers=headers, stream=True, timeout=30)
        if upstream.status_code in (403, 410):
            upstream.close()
            invalidate_audio_url(video_id)
            raise StreamResolveError('expired', 'Stream URL expired', 410)

    return upstream, url, skip
