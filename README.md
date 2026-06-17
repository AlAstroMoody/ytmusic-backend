# ytmusic-backend

Flask-бэкенд для поиска в YouTube Music и проксирования аудиопотока.

## Эндпоинты

- `GET /health` — health check
- `GET /search?q=...` — поиск песен (первая страница, JSON-массив треков)
- `GET /search?q=...&paginated=1` — то же, но объект `{ "tracks": [...], "continuation": "..." }`
- `GET /search/continue?continuation=...` — следующая страница поиска
- `POST /search/continue` — то же, body: `{ "continuation": "..." }`
- `GET /get-audio?videoId=...` — прямой URL на аудио (для отладки)
- `GET /stream?videoId=...` — стрим через прокси бэкенда
- `GET /liked` — лайкнутые треки (нужен auth-файл)
- `GET /playlists` — плейлисты (нужен auth-файл)

### Поиск с пагинацией

Используется встроенный `limit` из ytmusicapi (библиотека сама запрашивает continuation у YouTube Music).
Токен `continuation` — opaque-строка с номером страницы; клиенту передавать как есть.

Первая страница — обратно совместимый массив. Если есть ещё результаты, токен приходит в заголовке `X-Search-Continuation` (CORS-exposed для фронта).

```bash
# Первая страница (массив треков)
curl -s -D - "http://127.0.0.1:5000/search?q=linkin+park" | head

# Первая страница с объектом (удобно для нового фронта)
curl -s "http://127.0.0.1:5000/search?q=linkin+park&paginated=1"

# Следующая страница
curl -s "http://127.0.0.1:5000/search/continue?continuation=TOKEN_FROM_HEADER"
```

Формат трека:

```json
{
  "videoId": "BLZWkjBXfN8",
  "title": "In the End",
  "artists": [{ "id": "...", "name": "Linkin Park" }],
  "thumbnails": [{ "url": "...", "width": 120, "height": 120 }],
  "duration": "3:37",
  "duration_seconds": 217,
  "resultType": "song"
}
```

Ответ `/search/continue`:

```json
{
  "tracks": [ /* ... */ ],
  "continuation": "next_token_or_null"
}
```

Ошибки:

- пустой `q` или `continuation` → `400` + `{ "error": "..." }`
- невалидный/expired continuation → `400` / `410`
- ошибки YouTube Music API → `502`

## Локальный запуск

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Требования на сервере

`deploy-safe.sh` сам поставит `python3-venv` через apt, если venv не создаётся.  
Нужны: `git`, `sudo`, доступ к `apt`.

При первом деплое вручную:

```bash
sudo apt update
sudo apt install -y git python3
```

## Auth-файлы: что копировать на сервер

Да, auth-файл на сервере нужен, если используете эндпоинты с авторизацией.

- Только `/search` и `/stream` — auth-файл **не обязателен**
- `/liked` и `/playlists` — auth-файл **обязателен**

Два варианта:

1. **Рекомендуется:** `browser.json`
   - В `.env`: `AUTH_FILE=browser.json`
   - Создать через browser auth (шаги ниже)
2. `oauth.json`
   - В `.env`: `AUTH_FILE=oauth.json`
   - Плюс `YTM_CLIENT_ID` и `YTM_CLIENT_SECRET` в `.env`
   - Создать через oauth flow (шаги ниже)

На сервер скопировать вручную:

- `/opt/ytmusic-backend/.env`
- `/opt/ytmusic-backend/browser.json` **или** `/opt/ytmusic-backend/oauth.json`

Эти файлы в git не коммитить.

### Как создать `oauth.json`

1. Активируйте `venv`:

```bash
source venv/bin/activate
```

2. Запустите oauth setup:

```bash
ytmusicapi oauth --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
```

3. Перейдите по ссылке из терминала, подтвердите вход, вернитесь в терминал и нажмите Enter.  
   В текущей папке появится `oauth.json`.

Примечание: `YOUR_CLIENT_ID` и `YOUR_CLIENT_SECRET` берутся из Google Cloud Console (OAuth client типа **TVs and Limited Input devices**).

### Как создать `browser.json` (рекомендуется)

1. Активируйте `venv`:

```bash
source venv/bin/activate
```

2. Запустите setup:

```bash
ytmusicapi setup
```

3. Выберите вариант **browser**.
4. Откройте `music.youtube.com` под нужным аккаунтом.
5. В DevTools -> Network найдите любой запрос к YouTube Music и скопируйте **request headers**.
6. Вставьте headers в интерактивный setup.  
   В текущей папке появится `browser.json`.

## Безопасный повторный деплой (одна команда)

Скрипт **не трогает** nginx, SSL, certbot и порт 443.

```bash
chmod +x deploy-safe.sh && ./deploy-safe.sh
```

Что делает скрипт:

- `git pull --ff-only`
- установка зависимостей в `venv`
- перезапуск systemd-сервиса `ytmusic-backend`
- smoke-check локальных эндпоинтов

## Настройка systemd (один раз)

```bash
sudo cp ytmusic-backend.service /etc/systemd/system/ytmusic-backend.service
sudo systemctl daemon-reload
sudo systemctl enable ytmusic-backend
sudo systemctl start ytmusic-backend
```

Логи:

```bash
sudo journalctl -u ytmusic-backend -f
```

## Интеграция с nginx (рядом с bradio)

Если на том же сервере уже работает reverse proxy на `443`, добавьте только маршрут:

```nginx
location /api/yt/ {
    proxy_pass http://127.0.0.1:5000/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 60s;
    proxy_send_timeout 60s;
    proxy_read_timeout 300s;
}
```

Проверка:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -I "https://ВАШ_ДОМЕН/api/yt/search?q=test"
```

Во фронте:

- `/api/yt/search?q=...`
- `/api/yt/stream?videoId=...`
