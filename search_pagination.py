from __future__ import annotations

import base64
import json
from typing import Any

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

PAGE_SIZE = 20


class SearchPaginationError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def filter_songs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get('videoId')]


def encode_continuation(query: str, page: int) -> str:
    payload = json.dumps({'q': query, 'p': page}, separators=(',', ':'))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_continuation(token: str) -> tuple[str, int]:
    try:
        padding = '=' * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        data = json.loads(raw)
        query = data['q']
        page = data['p']
        if not isinstance(query, str) or not query or not isinstance(page, int) or page < 1:
            raise ValueError('invalid continuation payload')
        return query, page
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SearchPaginationError('Invalid or malformed continuation token') from exc


def _search_page(yt: YTMusic, query: str, page: int) -> tuple[list[dict[str, Any]], str | None]:
    # ytmusicapi.search(limit=N) сам ходит за continuation внутри библиотеки.
    # Берём на 1 трек больше, чтобы понять, есть ли следующая страница.
    limit = (page + 1) * PAGE_SIZE + 1
    try:
        results = yt.search(query, filter='songs', limit=limit)
    except (YTMusicServerError, YTMusicUserError) as exc:
        raise SearchPaginationError(str(exc), 502) from exc

    songs = filter_songs(results)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_songs = songs[start:end]
    has_more = len(songs) > end
    continuation = encode_continuation(query, page + 1) if has_more else None
    return page_songs, continuation


def search_songs_first_page(yt: YTMusic, query: str) -> tuple[list[dict[str, Any]], str | None]:
    return _search_page(yt, query, page=0)


def search_songs_continue(yt: YTMusic, continuation_token: str) -> tuple[list[dict[str, Any]], str | None]:
    query, page = decode_continuation(continuation_token)
    return _search_page(yt, query, page)
