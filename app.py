import json
import os

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
from ytmusicapi import YTMusic, OAuthCredentials
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError
import yt_dlp

from search_pagination import SearchPaginationError, search_songs_continue, search_songs_first_page
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

@app.route('/get-audio')
def get_audio():
    video_id = request.args.get('videoId')
    if not video_id:
        return jsonify({'error': 'Missing videoId parameter'}), 400
    try:
        audio_url = resolve_audio_url(video_id)
        return jsonify({'audioUrl': audio_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def resolve_audio_url(video_id: str) -> str:
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
        for f in info.get('formats', []):
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                url = f.get('url')
                if url:
                    return url
        url = info.get('url')
        if not url:
            raise Exception('No audio stream found')
        return url


@app.route('/stream')
def stream():
    video_id = request.args.get('videoId')
    if not video_id:
        return jsonify({'error': 'Missing videoId parameter'}), 400
    try:
        audio_url = resolve_audio_url(video_id)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    headers = {}
    if range_header := request.headers.get('Range'):
        headers['Range'] = range_header

    upstream = requests.get(audio_url, headers=headers, stream=True, timeout=30)
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
