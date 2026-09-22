from __future__ import annotations

from typing import Any

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError


class LikedFetchError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _is_auth_placeholder(playlist: dict[str, Any]) -> bool:
    description = playlist.get('description') or ''
    if 'Music you like in any YouTube app will show here' in description:
        return True
    track_count = playlist.get('trackCount') or 0
    tracks = playlist.get('tracks') or []
    if track_count > 0 and not tracks and not playlist.get('owned', True):
        return True
    return False


def fetch_liked_playlist(yt: YTMusic, *, limit: int) -> dict[str, Any]:
    try:
        playlist = yt.get_liked_songs(limit=limit)
    except (YTMusicServerError, YTMusicUserError) as exc:
        raise LikedFetchError('upstream', str(exc), 502) from exc

    tracks = playlist.get('tracks') or []
    if tracks:
        return playlist

    if _is_auth_placeholder(playlist):
        raise LikedFetchError(
            'auth_expired',
            'YouTube Music auth expired or invalid. Re-export browser.json on the server.',
            401,
        )

    try:
        library_tracks = yt.get_library_songs(limit=limit, order='recently_added')
    except (YTMusicServerError, YTMusicUserError) as exc:
        raise LikedFetchError('upstream', str(exc), 502) from exc

    if library_tracks:
        playlist['tracks'] = library_tracks
        return playlist

    track_count = playlist.get('trackCount') or 0
    if track_count > 0:
        raise LikedFetchError(
            'auth_expired',
            'YouTube Music auth expired or invalid. Re-export browser.json on the server.',
            401,
        )

    return playlist
