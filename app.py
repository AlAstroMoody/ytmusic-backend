import json
import os

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
from ytmusicapi import YTMusic, OAuthCredentials
import yt_dlp

load_dotenv()

app = Flask(__name__)
CORS(app)

AUTH_FILE = os.getenv('AUTH_FILE') or os.getenv('OAUTH_FILE', 'browser.json')


def load_auth_client() -> YTMusic | None:
    if not os.path.exists(AUTH_FILE):
        return None

    with open(AUTH_FILE, encoding='utf-8') as f:
        auth_data = json.load(f)

    if 'access_token' in auth_data:
        client_id = os.getenv('YTM_CLIENT_ID')
        client_secret = os.getenv('YTM_CLIENT_SECRET')
        if not client_id or not client_secret:
            raise RuntimeError(
                'OAuth file detected. Set YTM_CLIENT_ID and YTM_CLIENT_SECRET in .env'
            )
        return YTMusic(
            AUTH_FILE,
            oauth_credentials=OAuthCredentials(
                client_id=client_id,
                client_secret=client_secret,
            ),
        )

    return YTMusic(AUTH_FILE)


yt_public = YTMusic()
yt_auth = load_auth_client()


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/search')
def search():
    query = request.args.get('q')
    if not query:
        return jsonify({'error': 'Missing query parameter'}), 400
    try:
        results = yt_public.search(query)
        songs = [item for item in results if item.get('resultType') == 'song']
        return jsonify(songs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
