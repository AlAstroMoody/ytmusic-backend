from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import requests
import yt_dlp

from ttl_cache import TtlLruCache

STREAM_URL_CACHE_MAX = int(os.getenv('STREAM_URL_CACHE_MAX', '96'))
STREAM_URL_CACHE_TTL = float(os.getenv('STREAM_URL_CACHE_TTL', '600'))
STREAM_FILE_CACHE_DIR = Path(os.getenv('STREAM_FILE_CACHE_DIR', '/tmp/ytmusic-stream-cache'))
STREAM_FILE_CACHE_MAX = int(os.getenv('STREAM_FILE_CACHE_MAX', '48'))
STREAM_FILE_MIN_BYTES = int(os.getenv('STREAM_FILE_MIN_BYTES', str(64 * 1024)))

# googlevideo rejects open-ended Range (bytes=N-); cap each upstream request to 1 MiB.
_UPSTREAM_RANGE_CHUNK = 1024 * 1024

_AUDIO_EXTENSIONS = ('m4a', 'webm', 'opus', 'mp4', 'ogg')

_PLAYER_CLIENT_STRATEGIES = (
    ['default', '-android_sdkless'],
    ['web_safari', '-android_sdkless'],
    ['web_creator', '-android_sdkless'],
)

_url_cache: TtlLruCache[str, dict[str, str]] = TtlLruCache(STREAM_URL_CACHE_MAX, STREAM_URL_CACHE_TTL)
_download_locks: dict[str, threading.Lock] = {}
_download_locks_guard = threading.Lock()


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


def _ydl_opts(player_clients: list[str], *, outtmpl: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': player_clients}},
    }
    if outtmpl is not None:
        opts['outtmpl'] = outtmpl
        opts['noplaylist'] = True
    return opts


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
    with yt_dlp.YoutubeDL(_ydl_opts(player_clients)) as ydl:
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
    # Single byte at 1048576 can still 206 on DASH init URLs; require a real chunk past 1 MiB.
    return _probe_range(url, headers, 'bytes=1048576-1114111')


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


def stream_delivery_mode(video_id: str) -> Literal['direct', 'file']:
    target = resolve_stream_target(video_id)
    if _url_is_fully_proxyable(target['url'], target['headers']):
        return 'direct'
    return 'file'


def _download_lock(video_id: str) -> threading.Lock:
    with _download_locks_guard:
        lock = _download_locks.get(video_id)
        if lock is None:
            lock = threading.Lock()
            _download_locks[video_id] = lock
        return lock


def _find_cached_file(video_id: str) -> Path | None:
    for ext in _AUDIO_EXTENSIONS:
        path = STREAM_FILE_CACHE_DIR / f'{video_id}.{ext}'
        if path.is_file() and path.stat().st_size >= STREAM_FILE_MIN_BYTES:
            return path
    return None


def _cleanup_partial_downloads(video_id: str) -> None:
    for ext in _AUDIO_EXTENSIONS:
        path = STREAM_FILE_CACHE_DIR / f'{video_id}.{ext}'
        if path.is_file() and path.stat().st_size < STREAM_FILE_MIN_BYTES:
            path.unlink(missing_ok=True)
    partial = STREAM_FILE_CACHE_DIR / f'{video_id}.part'
    partial.unlink(missing_ok=True)


def _enforce_file_cache_limits() -> None:
    files = [
        path
        for path in STREAM_FILE_CACHE_DIR.iterdir()
        if path.is_file() and path.suffix.lstrip('.') in _AUDIO_EXTENSIONS
    ]
    if len(files) <= STREAM_FILE_CACHE_MAX:
        return
    files.sort(key=lambda path: path.stat().st_mtime)
    for path in files[: len(files) - STREAM_FILE_CACHE_MAX]:
        path.unlink(missing_ok=True)


def _download_stream_file(video_id: str, player_clients: list[str]) -> None:
    STREAM_FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    outtmpl = str(STREAM_FILE_CACHE_DIR / video_id) + '.%(ext)s'
    with yt_dlp.YoutubeDL(_ydl_opts(player_clients, outtmpl=outtmpl)) as ydl:
        ydl.download([f'https://www.youtube.com/watch?v={video_id}'])


def resolve_stream_file(video_id: str) -> Path:
    cached = _find_cached_file(video_id)
    if cached:
        return cached

    with _download_lock(video_id):
        cached = _find_cached_file(video_id)
        if cached:
            return cached

        last_error: Exception | None = None
        for player_clients in _PLAYER_CLIENT_STRATEGIES:
            _cleanup_partial_downloads(video_id)
            try:
                _download_stream_file(video_id, player_clients)
            except Exception as exc:
                last_error = exc
                continue

            cached = _find_cached_file(video_id)
            if cached:
                _enforce_file_cache_limits()
                return cached

        _cleanup_partial_downloads(video_id)
        if last_error is not None:
            raise _classify_ytdlp_error(last_error) from last_error
        raise StreamResolveError('unavailable', 'Download produced no audio file', 502)


def guess_audio_mimetype(path: Path) -> str:
    ext = path.suffix.lower().lstrip('.')
    if ext in ('m4a', 'mp4'):
        return 'audio/mp4'
    if ext == 'webm':
        return 'audio/webm'
    if ext == 'opus':
        return 'audio/opus'
    if ext == 'ogg':
        return 'audio/ogg'
    return 'application/octet-stream'


def _normalize_upstream_range(range_header: str | None) -> str:
    if not range_header:
        end = _UPSTREAM_RANGE_CHUNK - 1
        return f'bytes=0-{end}'

    value = range_header.strip()
    if re.fullmatch(r'bytes=0-', value, re.IGNORECASE):
        end = _UPSTREAM_RANGE_CHUNK - 1
        return f'bytes=0-{end}'

    open_ended = re.fullmatch(r'bytes=(\d+)-', value, re.IGNORECASE)
    if open_ended:
        start = int(open_ended.group(1))
        end = start + _UPSTREAM_RANGE_CHUNK - 1
        return f'bytes={start}-{end}'

    return value


def _upstream_headers(stream_headers: dict[str, str], range_header: str | None) -> dict[str, str]:
    headers = dict(stream_headers)
    headers['Range'] = _normalize_upstream_range(range_header)
    return headers


def build_proxy_headers(upstream_headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    allowed = ('content-type', 'content-length', 'content-range', 'accept-ranges')
    return {
        key: value
        for key, value in upstream_headers.items()
        if key.lower() in allowed
    }


def iter_upstream_body(upstream: requests.Response) -> Iterator[bytes]:
    yield from upstream.iter_content(chunk_size=64 * 1024)


def open_audio_upstream(
    video_id: str,
    range_header: str | None = None,
) -> requests.Response:
    """Proxy a fully proxyable googlevideo URL; retry once on 403/410."""
    target = resolve_stream_target(video_id)
    if not _url_is_fully_proxyable(target['url'], target['headers']):
        raise StreamResolveError('unavailable', 'Direct stream URL is truncated', 502)

    headers = _upstream_headers(target['headers'], range_header)
    upstream = requests.get(target['url'], headers=headers, stream=True, timeout=30)

    if upstream.status_code in (403, 410):
        upstream.close()
        invalidate_audio_url(video_id)
        try:
            target = resolve_stream_target(video_id, bypass_cache=True)
        except StreamResolveError:
            raise StreamResolveError('expired', 'Stream URL expired and refresh failed', 410) from None
        if not _url_is_fully_proxyable(target['url'], target['headers']):
            raise StreamResolveError('expired', 'Stream URL expired', 410)
        headers = _upstream_headers(target['headers'], range_header)
        upstream = requests.get(target['url'], headers=headers, stream=True, timeout=30)
        if upstream.status_code in (403, 410):
            upstream.close()
            invalidate_audio_url(video_id)
            raise StreamResolveError('expired', 'Stream URL expired', 410)

    return upstream
