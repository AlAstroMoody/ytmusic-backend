from __future__ import annotations

import base64
import json
from typing import Any, Literal

import yt_dlp
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from stream_service import ytdlp_lookup_opts

PAGE_SIZE = 20
SearchSource = Literal['ytm', 'dlp']


class SearchPaginationError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def filter_songs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get('videoId')]


def encode_continuation(query: str, page: int, source: SearchSource = 'ytm') -> str:
    payload = json.dumps({'q': query, 'p': page, 's': source}, separators=(',', ':'))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_continuation(token: str) -> tuple[str, int, SearchSource]:
    try:
        padding = '=' * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        data = json.loads(raw)
        query = data['q']
        page = data['p']
        source = data.get('s', 'ytm')
        if source not in ('ytm', 'dlp'):
            source = 'ytm'
        if not isinstance(query, str) or not query or not isinstance(page, int) or page < 1:
            raise ValueError('invalid continuation payload')
        return query, page, source
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SearchPaginationError('Invalid or malformed continuation token') from exc


def _run_search(yt: YTMusic, query: str, *, limit: int) -> list[dict[str, Any]]:
    try:
        return yt.search(query, filter='songs', limit=limit)
    except (YTMusicServerError, YTMusicUserError) as exc:
        raise SearchPaginationError(str(exc), 502) from exc


def _fallback_search(yt: YTMusic, query: str, *, limit: int) -> list[dict[str, Any]]:
    try:
        results = yt.search(query, limit=min(limit, 20))
    except (YTMusicServerError, YTMusicUserError) as exc:
        raise SearchPaginationError(str(exc), 502) from exc

    playable: list[dict[str, Any]] = []
    for item in results:
        if item.get('resultType') not in ('song', 'video'):
            continue
        if item.get('videoId'):
            playable.append(item)
    return playable


def _collect_songs_ytm(yt: YTMusic, query: str, *, limit: int) -> list[dict[str, Any]]:
    songs = filter_songs(_run_search(yt, query, limit=limit))
    if songs:
        return songs
    return _fallback_search(yt, query, limit=limit)


def _entry_to_song(entry: dict[str, Any]) -> dict[str, Any] | None:
    video_id = entry.get('id') or entry.get('video_id')
    if not video_id:
        url = entry.get('url') or entry.get('webpage_url') or ''
        if 'v=' in url:
            video_id = url.split('v=')[-1].split('&')[0]
    if not video_id:
        return None

    artist_name = entry.get('artist') or entry.get('uploader') or entry.get('channel')
    artists = [{'name': artist_name}] if artist_name else []

    thumbnails = []
    thumb = entry.get('thumbnail')
    if thumb:
        thumbnails = [{'url': thumb}]

    duration_seconds = entry.get('duration')
    duration = entry.get('duration_string')
    if duration_seconds is not None and not duration:
        mins, secs = divmod(int(duration_seconds), 60)
        duration = f'{mins}:{secs:02d}'

    return {
        'videoId': video_id,
        'title': entry.get('title'),
        'artists': artists,
        'thumbnails': thumbnails,
        'duration': duration,
        'duration_seconds': duration_seconds,
        'resultType': 'song',
    }


def _collect_songs_ytdlp(query: str, *, limit: int) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 100))
    try:
        with yt_dlp.YoutubeDL(ytdlp_lookup_opts()) as ydl:
            info = ydl.extract_info(f'ytsearch{capped}:{query}', download=False)
    except Exception as exc:
        raise SearchPaginationError(str(exc), 502) from exc

    entries = info.get('entries') if isinstance(info, dict) else None
    if not entries:
        return []

    songs: list[dict[str, Any]] = []
    for entry in entries:
        if not entry:
            continue
        song = _entry_to_song(entry)
        if song:
            songs.append(song)
    return songs


def _paginate(songs: list[dict[str, Any]], query: str, page: int, source: SearchSource) -> tuple[list[dict[str, Any]], str | None]:
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_songs = songs[start:end]
    has_more = len(songs) > end
    continuation = encode_continuation(query, page + 1, source=source) if has_more else None
    return page_songs, continuation


def _search_page_ytm(yt: YTMusic, query: str, page: int) -> tuple[list[dict[str, Any]], str | None]:
    limit = (page + 1) * PAGE_SIZE + 1
    songs = _collect_songs_ytm(yt, query, limit=limit)
    return _paginate(songs, query, page, 'ytm')


def _search_page_ytdlp(query: str, page: int) -> tuple[list[dict[str, Any]], str | None]:
    limit = (page + 1) * PAGE_SIZE + 1
    songs = _collect_songs_ytdlp(query, limit=limit)
    return _paginate(songs, query, page, 'dlp')


def _search_first_page(clients: list[YTMusic], query: str) -> tuple[list[dict[str, Any]], str | None]:
    last_error: SearchPaginationError | None = None
    for yt in clients:
        try:
            page_songs, continuation = _search_page_ytm(yt, query, page=0)
        except SearchPaginationError as exc:
            last_error = exc
            continue
        if page_songs:
            return page_songs, continuation

    try:
        page_songs, continuation = _search_page_ytdlp(query, page=0)
    except SearchPaginationError:
        if last_error is not None:
            raise last_error
        raise
    if page_songs:
        return page_songs, continuation
    if last_error is not None:
        raise last_error
    return [], None


def search_songs_first_page(clients: list[YTMusic], query: str) -> tuple[list[dict[str, Any]], str | None]:
    return _search_first_page(clients, query)


def search_songs_continue(
    clients: list[YTMusic],
    continuation_token: str,
) -> tuple[list[dict[str, Any]], str | None]:
    query, page, source = decode_continuation(continuation_token)
    if source == 'dlp':
        return _search_page_ytdlp(query, page)

    last_error: SearchPaginationError | None = None
    for yt in clients:
        try:
            page_songs, continuation = _search_page_ytm(yt, query, page)
        except SearchPaginationError as exc:
            last_error = exc
            continue
        if page_songs:
            return page_songs, continuation

    page_songs, continuation = _search_page_ytdlp(query, page)
    if page_songs:
        return page_songs, continuation
    if last_error is not None:
        raise last_error
    return [], None
