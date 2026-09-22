import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from ytmusicapi import YTMusic, OAuthCredentials
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from liked_service import LikedFetchError, fetch_liked_playlist
from search_pagination import SearchPaginationError, search_songs_continue, search_songs_first_page
from stream_service import StreamResolveError, guess_audio_mimetype, resolve_audio_url, resolve_stream_file
from track_normalize import normalize_tracks

load_dotenv()

app = Flask(__name__)
CORS(app, expose_headers=['X-Search-Continuation'])

APP_DIR = Path(__file__).resolve().parent


def auth_file_path() -> Path:
    raw = os.getenv('AUTH_FILE') or os.getenv('OAUTH_FILE', 'browser.json')
    path = Path(raw)
    if not path.is_absolute():
        path = APP_DIR / path
    return path


def load_auth_client() -> YTMusic | None:
    auth_path = auth_file_path()
    if not auth_path.is_file():
        print(f'INFO: auth file not found at {auth_path} — /liked and /playlists need browser.json')
        return None

    try:
        with auth_path.open(encoding='utf-8') as f:
            auth_data = json.load(f)

        auth_str = str(auth_path)
        if 'access_token' in auth_data:
            client_id = os.getenv('YTM_CLIENT_ID')
            client_secret = os.getenv('YTM_CLIENT_SECRET')
            if not client_id or not client_secret:
                print(
                    'WARNING: OAuth file detected but YTM_CLIENT_ID/YTM_CLIENT_SECRET '
                    'missing; auth endpoints disabled'
                )
                return None
            return YTMusic(
                auth_str,
                oauth_credentials=OAuthCredentials(
                    client_id=client_id,
                    client_secret=client_secret,
                ),
            )

        return YTMusic(auth_str)
    except Exception as exc:
        print(f'WARNING: failed to load auth from {auth_path}: {exc}')
        print('Auth endpoints (/liked, /playlists) disabled; public search/stream still work')
        return None


yt_public = YTMusic()
yt_auth: YTMusic | None = None


def get_auth_client() -> YTMusic | None:
    global yt_auth
    if yt_auth is not None:
        return yt_auth
    yt_auth = load_auth_client()
    return yt_auth


yt_auth = load_auth_client()


def yt_search_clients() -> list[YTMusic]:
    clients: list[YTMusic] = []
    auth = get_auth_client()
    if auth is not None:
        clients.append(auth)
    clients.append(yt_public)
    return clients


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Missing query parameter'}), 400

    paginated = request.args.get('paginated', '').lower() in ('1', 'true', 'yes')

    try:
        songs, continuation = search_songs_first_page(yt_search_clients(), query)
    except SearchPaginationError as exc:
        return jsonify({'error': str(exc)}), exc.status_code
    except (YTMusicServerError, YTMusicUserError, requests.RequestException) as exc:
        return jsonify({'error': str(exc)}), 502

    tracks = normalize_tracks(songs)

    if paginated:
        return jsonify({
            'tracks': tracks,
            'continuation': continuation,
        })

    response = jsonify(tracks)
    if continuation:
        response.headers['X-Search-Continuation'] = continuation
    return response


@app.route('/search/continue', methods=['GET', 'POST'])
def search_continue():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        continuation = str(payload.get('continuation', '')).strip()
    else:
        continuation = request.args.get('continuation', '').strip()

    if not continuation:
        return jsonify({'error': 'Missing continuation parameter'}), 400

    try:
        songs, next_continuation = search_songs_continue(yt_search_clients(), continuation)
    except SearchPaginationError as exc:
        return jsonify({'error': str(exc)}), exc.status_code
    except (YTMusicServerError, YTMusicUserError, requests.RequestException) as exc:
        return jsonify({'error': str(exc)}), 502

    return jsonify({
        'tracks': normalize_tracks(songs),
        'continuation': next_continuation,
    })


@app.route('/suggest')
def suggest():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Missing query parameter'}), 400

    try:
        suggestions = yt_public.get_search_suggestions(query)
    except YTMusicUserError as exc:
        return jsonify({'error': str(exc)}), 400
    except (YTMusicServerError, requests.RequestException) as exc:
        return jsonify({'error': str(exc)}), 502

    return jsonify({'suggestions': suggestions})


@app.route('/radio')
def radio():
    video_id = request.args.get('videoId', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing videoId parameter'}), 400

    limit_raw = request.args.get('limit', '25')
    try:
        limit = max(1, min(int(limit_raw), 50))
    except ValueError:
        return jsonify({'error': 'Invalid limit parameter'}), 400

    radio_mode = request.args.get('radio', '1').lower() not in ('0', 'false', 'no')

    try:
        playlist = yt_public.get_watch_playlist(
            videoId=video_id,
            limit=limit,
            radio=radio_mode,
        )
    except YTMusicUserError as exc:
        return jsonify({'error': str(exc)}), 400
    except (YTMusicServerError, requests.RequestException) as exc:
        return jsonify({'error': str(exc)}), 502

    tracks = normalize_tracks(playlist.get('tracks') or [])
    return jsonify({
        'tracks': tracks,
        'playlistId': playlist.get('playlistId'),
        'videoId': video_id,
    })


def require_auth():
    if get_auth_client() is None:
        path = auth_file_path()
        return jsonify({
            'error': (
                f'Auth not configured. Run `ytmusicapi browser` locally, copy the file to '
                f'{path} on the server (or set AUTH_FILE in .env), then retry.'
            ),
            'authPath': str(path),
        }), 503
    return None


@app.route('/liked')
def get_liked():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    limit_raw = request.args.get('limit', '200')
    try:
        limit = max(1, min(int(limit_raw), 500))
    except ValueError:
        return jsonify({'error': 'Invalid limit parameter'}), 400

    try:
        liked_songs = fetch_liked_playlist(get_auth_client(), limit=limit)
    except LikedFetchError as exc:
        return jsonify({'error': exc.message, 'code': exc.code}), exc.status_code
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

    liked_songs['tracks'] = normalize_tracks(liked_songs.get('tracks') or [])
    return jsonify(liked_songs)


@app.route('/playlists')
def get_playlists():
    auth_error = require_auth()
    if auth_error:
        return auth_error
    try:
        playlists = get_auth_client().get_library_playlists()
        return jsonify(playlists)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _normalize_playlist_id(playlist_id: str) -> str:
    pid = playlist_id.strip()
    if pid.startswith('VL'):
        return pid[2:]
    return pid


@app.route('/playlist')
@app.route('/playlist/<playlist_id>')
def get_playlist(playlist_id: str | None = None):
    raw_id = (playlist_id or request.args.get('id', '')).strip()
    if not raw_id:
        return jsonify({'error': 'Missing playlist id'}), 400

    limit_raw = request.args.get('limit', '100')
    try:
        limit = max(1, min(int(limit_raw), 200))
    except ValueError:
        return jsonify({'error': 'Invalid limit parameter'}), 400

    pid = _normalize_playlist_id(raw_id)

    try:
        # Public / catalog playlists — no auth required.
        playlist = yt_public.get_playlist(pid, limit=limit)
    except YTMusicUserError as exc:
        return jsonify({'error': str(exc)}), 400
    except (YTMusicServerError, requests.RequestException) as exc:
        return jsonify({'error': str(exc)}), 502
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502

    tracks = normalize_tracks(playlist.get('tracks') or [])
    return jsonify({
        'id': playlist.get('id') or pid,
        'title': playlist.get('title'),
        'author': playlist.get('author'),
        'thumbnails': playlist.get('thumbnails') or [],
        'trackCount': playlist.get('trackCount'),
        'duration': playlist.get('duration'),
        'duration_seconds': playlist.get('duration_seconds'),
        'tracks': tracks,
    })


@app.route('/get-audio')
def get_audio():
    """Deprecated: use GET /stream?videoId= as <audio src>. Kept for debug."""
    video_id = request.args.get('videoId', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing videoId parameter'}), 400
    try:
        audio_url = resolve_audio_url(video_id)
        response = jsonify({'audioUrl': audio_url, 'deprecated': True})
        response.headers['Deprecation'] = 'true'
        return response
    except StreamResolveError as exc:
        return jsonify({'error': exc.message, 'code': exc.code}), exc.status_code


@app.route('/stream')
def stream():
    """Media endpoint for <audio src>. Do not change this contract."""
    video_id = request.args.get('videoId', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing videoId parameter', 'code': 'bad_request'}), 400

    try:
        path = resolve_stream_file(video_id)
        response = send_file(
            path,
            mimetype=guess_audio_mimetype(path),
            conditional=True,
            download_name=path.name,
        )
        response.headers['Cache-Control'] = 'no-store'
        return response
    except StreamResolveError as exc:
        response = jsonify({'error': exc.message, 'code': exc.code})
        response.headers['Cache-Control'] = 'no-store'
        return response, exc.status_code

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, host='0.0.0.0', port=5000)
