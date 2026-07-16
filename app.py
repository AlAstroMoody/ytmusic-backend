import json
import os

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
from ytmusicapi import YTMusic, OAuthCredentials
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from search_pagination import SearchPaginationError, search_songs_continue, search_songs_first_page
from stream_service import StreamResolveError, open_audio_upstream, resolve_audio_url
from track_normalize import normalize_tracks

load_dotenv()

app = Flask(__name__)
CORS(app, expose_headers=['X-Search-Continuation'])

AUTH_FILE = os.getenv('AUTH_FILE') or os.getenv('OAUTH_FILE', 'browser.json')


def load_auth_client() -> YTMusic | None:
    if not os.path.exists(AUTH_FILE):
        return None

    try:
        with open(AUTH_FILE, encoding='utf-8') as f:
            auth_data = json.load(f)

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
                AUTH_FILE,
                oauth_credentials=OAuthCredentials(
                    client_id=client_id,
                    client_secret=client_secret,
                ),
            )

        return YTMusic(AUTH_FILE)
    except Exception as exc:
        print(f'WARNING: failed to load auth from {AUTH_FILE}: {exc}')
        print('Auth endpoints (/liked, /playlists) disabled; public search/stream still work')
        return None


yt_public = YTMusic()
yt_auth = load_auth_client()


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
        songs, continuation = search_songs_first_page(yt_public, query)
    except SearchPaginationError as exc:
        return jsonify({'error': str(exc)}), exc.status_code
    except (YTMusicServerError, YTMusicUserError, requests.RequestException) as exc:
        return jsonify({'error': str(exc)}), 502

    if paginated:
        return jsonify({
            'tracks': songs,
            'continuation': continuation,
        })

    response = jsonify(songs)
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
        songs, next_continuation = search_songs_continue(yt_public, continuation)
    except SearchPaginationError as exc:
        return jsonify({'error': str(exc)}), exc.status_code
    except (YTMusicServerError, YTMusicUserError, requests.RequestException) as exc:
        return jsonify({'error': str(exc)}), 502

    return jsonify({
        'tracks': songs,
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
    if yt_auth is None:
        return jsonify({
            'error': (
                'Auth not configured. Create browser.json via '
                'ytmusicapi setup (browser auth) and set AUTH_FILE in .env'
            ),
        }), 503
    return None


@app.route('/liked')
def get_liked():
    auth_error = require_auth()
    if auth_error:
        return auth_error
    try:
        liked_songs = yt_auth.get_liked_songs()
        return jsonify(liked_songs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/playlists')
def get_playlists():
    auth_error = require_auth()
    if auth_error:
        return auth_error
    try:
        playlists = yt_auth.get_library_playlists()
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
        upstream, _url = open_audio_upstream(
            video_id,
            range_header=request.headers.get('Range'),
        )
    except StreamResolveError as exc:
        return jsonify({'error': exc.message, 'code': exc.code}), exc.status_code
    except requests.RequestException as exc:
        return jsonify({'error': str(exc), 'code': 'upstream'}), 502

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in ('content-type', 'content-length', 'content-range', 'accept-ranges')
    }

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
        status=upstream.status_code,
        headers=response_headers,
    )

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, host='0.0.0.0', port=5000)
