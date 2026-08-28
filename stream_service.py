from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

import yt_dlp

STREAM_FILE_CACHE_DIR = Path(os.getenv('STREAM_FILE_CACHE_DIR', '/tmp/ytmusic-stream-cache'))
STREAM_FILE_CACHE_MAX = int(os.getenv('STREAM_FILE_CACHE_MAX', '48'))
STREAM_FILE_MIN_BYTES = int(os.getenv('STREAM_FILE_MIN_BYTES', str(16 * 1024)))

_AUDIO_EXTENSIONS = ('m4a', 'webm', 'opus', 'mp4', 'ogg')

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
    if 'sign in' in text or 'login' in text or 'private' in text:
        return StreamResolveError('unavailable', str(exc), 404)
    if 'not available' in text or 'unavailable' in text or 'removed' in text:
        return StreamResolveError('unavailable', str(exc), 404)
    if 'geo' in text or 'country' in text or 'not made this video available' in text:
        return StreamResolveError('geo', str(exc), 451)
    return StreamResolveError('upstream', str(exc), 502)


def _js_runtime_opts() -> dict[str, Any]:
    """yt-dlp needs an external JS runtime for full YouTube support (same for pip and CLI)."""
    runtimes: dict[str, dict[str, str]] = {}

    try:
        import deno

        runtimes['deno'] = {'path': deno.find_deno_bin()}
    except Exception:
        deno_path = shutil.which('deno')
        if deno_path:
            runtimes['deno'] = {'path': deno_path}

    if not runtimes:
        node_path = shutil.which('node')
        if node_path:
            runtimes['node'] = {'path': node_path}

    if not runtimes:
        return {}
    return {'js_runtimes': runtimes}


def _ydl_opts(*, outtmpl: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extractor_args': {
            'youtube': {'player_client': ['default', '-android_sdkless']},
        },
        **_js_runtime_opts(),
    }
    if outtmpl is not None:
        opts['outtmpl'] = outtmpl
    return opts


def _format_filesize(info: dict[str, Any]) -> int | None:
    for key in ('filesize', 'filesize_approx'):
        value = info.get(key)
        if value:
            return int(value)
    format_id = info.get('format_id')
    for fmt in info.get('formats') or []:
        if fmt.get('format_id') == format_id:
            for key in ('filesize', 'filesize_approx'):
                value = fmt.get(key)
                if value:
                    return int(value)
    return None


def _watch_url(video_id: str) -> str:
    return f'https://www.youtube.com/watch?v={video_id}'


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
    for path in STREAM_FILE_CACHE_DIR.glob(f'{video_id}.*'):
        if path.suffix.lstrip('.') not in _AUDIO_EXTENSIONS:
            path.unlink(missing_ok=True)


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


def resolve_stream_file(video_id: str) -> Path:
    """Download audio with yt-dlp (handles DASH/HLS internally) and cache on disk."""
    cached = _find_cached_file(video_id)
    if cached:
        return cached

    with _download_lock(video_id):
        cached = _find_cached_file(video_id)
        if cached:
            return cached

        _cleanup_partial_downloads(video_id)
        STREAM_FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        outtmpl = str(STREAM_FILE_CACHE_DIR / video_id) + '.%(ext)s'
        watch_url = _watch_url(video_id)

        try:
            with yt_dlp.YoutubeDL(_ydl_opts(outtmpl=outtmpl)) as ydl:
                info = ydl.extract_info(watch_url, download=False)
                expected = _format_filesize(info or {})
                if ydl.download([watch_url]) != 0:
                    raise StreamResolveError('upstream', 'yt-dlp download failed', 502)
        except StreamResolveError:
            _cleanup_partial_downloads(video_id)
            raise
        except Exception as exc:
            _cleanup_partial_downloads(video_id)
            raise _classify_ytdlp_error(exc) from exc

        path = _find_cached_file(video_id)
        if not path:
            raise StreamResolveError('unavailable', 'Download produced no audio file', 502)

        if expected and path.stat().st_size < expected * 0.9:
            path.unlink(missing_ok=True)
            raise StreamResolveError(
                'truncated',
                f'Downloaded file too small ({path.stat().st_size} < {expected})',
                502,
            )

        _enforce_file_cache_limits()
        return path


def resolve_audio_url(video_id: str) -> str:
    """Deprecated debug helper: returns a direct format URL from yt-dlp."""
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(_watch_url(video_id), download=False)
    url = (info or {}).get('url')
    if not url:
        raise StreamResolveError('unavailable', 'No audio stream found', 404)
    return url


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
