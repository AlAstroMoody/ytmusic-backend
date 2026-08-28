from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp

from ttl_cache import TtlLruCache

STREAM_URL_CACHE_MAX = int(os.getenv('STREAM_URL_CACHE_MAX', '96'))
STREAM_URL_CACHE_TTL = float(os.getenv('STREAM_URL_CACHE_TTL', '600'))

_url_cache: TtlLruCache[str, str] = TtlLruCache(STREAM_URL_CACHE_MAX, STREAM_URL_CACHE_TTL)

# Android/iOS player URLs are often DASH init segments: tiny Range works, full GET 403/410.
_PLAYER_CLIENT_STRATEGIES = (
    ['web', '-android_sdkless'],
    ['web_safari', '-android_sdkless'],
    ['default', '-android_sdkless'],
)


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


def _auth_cookie() -> str | None:
    auth_file = os.getenv('AUTH_FILE') or os.getenv('OAUTH_FILE', 'browser.json')
    if not os.path.exists(auth_file):
        return None
    try:
        with open(auth_file, encoding='utf-8') as handle:
            auth = json.load(handle)
        cookie = auth.get('Cookie') or auth.get('cookie')
        return cookie if isinstance(cookie, str) and cookie.strip() else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


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


def _pick_audio_url(info: dict[str, Any]) -> str | None:
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
    return audio_only[0].get('url')


def _is_mobile_player_url(url: str) -> bool:
    client = (parse_qs(urlparse(url).query).get('c') or [''])[0]
    return client.startswith(('ANDROID', 'IOS'))


def _extract_info(video_id: str, player_clients: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        'format': '140/bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]/best',
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': player_clients}},
    }
    cookie = _auth_cookie()
    if cookie:
        opts['http_headers'] = {'Cookie': cookie}

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)


def _googlevideo_headers(range_header: str | None) -> dict[str, str]:
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
    }
    if range_header:
        headers['Range'] = range_header
    return headers


def _url_supports_full_fetch(url: str) -> bool:
    if _is_mobile_player_url(url):
        return False
    try:
        upstream = requests.get(
            url,
            headers=_googlevideo_headers(None),
            stream=True,
            timeout=8,
        )
    except requests.RequestException:
        return False
    ok = upstream.status_code == 200
    upstream.close()
    return ok


def resolve_audio_url(video_id: str, *, bypass_cache: bool = False) -> str:
    if not bypass_cache:
        cached = _url_cache.get(video_id)
        if cached and _url_supports_full_fetch(cached):
            return cached
        if cached:
            _url_cache.invalidate(video_id)

    last_error: Exception | None = None
    for player_clients in _PLAYER_CLIENT_STRATEGIES:
        try:
            info = _extract_info(video_id, player_clients)
        except Exception as exc:
            last_error = exc
            continue

        url = _pick_audio_url(info or {})
        if not url or not _url_supports_full_fetch(url):
            continue

        _url_cache.set(video_id, url)
        return url

    if last_error is not None:
        raise _classify_ytdlp_error(last_error) from last_error
    raise StreamResolveError('unavailable', 'No proxyable audio stream found', 404)


def invalidate_audio_url(video_id: str) -> None:
    _url_cache.invalidate(video_id)


def open_audio_upstream(
    video_id: str,
    range_header: str | None = None,
) -> tuple[requests.Response, str]:
    """Open googlevideo stream; retry once with fresh URL on 403/410."""
    headers = _googlevideo_headers(range_header)

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
