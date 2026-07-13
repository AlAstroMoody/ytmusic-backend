from __future__ import annotations

from typing import Any


def duration_to_seconds(duration: str | None) -> int | None:
    if not duration or not isinstance(duration, str):
        return None
    parts = duration.strip().split(':')
    if not parts or not all(p.isdigit() for p in parts):
        return None
    values = [int(p) for p in parts]
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0] * 60 + values[1]
    if len(values) == 3:
        return values[0] * 3600 + values[1] * 60 + values[2]
    return None


def normalize_track(item: dict[str, Any]) -> dict[str, Any] | None:
    video_id = item.get('videoId')
    if not video_id:
        return None

    duration = item.get('duration') or item.get('length')
    duration_seconds = item.get('duration_seconds')
    if duration_seconds is None:
        duration_seconds = duration_to_seconds(duration)

    thumbnails = item.get('thumbnails') or item.get('thumbnail') or []
    if isinstance(thumbnails, dict):
        thumbnails = [thumbnails]

    track: dict[str, Any] = {
        'videoId': video_id,
        'title': item.get('title'),
        'artists': item.get('artists') or [],
        'thumbnails': thumbnails,
        'duration': duration,
        'duration_seconds': duration_seconds,
    }
    if item.get('resultType'):
        track['resultType'] = item['resultType']
    if item.get('album') is not None:
        track['album'] = item['album']
    return track


def normalize_tracks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    for item in items:
        track = normalize_track(item)
        if track:
            tracks.append(track)
    return tracks
