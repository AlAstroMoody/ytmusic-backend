#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/ytmusic-backend"
SERVICE_NAME="ytmusic-backend"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Safe deploy for ${SERVICE_NAME} (no nginx/ssl changes)"

if [[ ! -d "${APP_DIR}" ]]; then
  echo "ERROR: ${APP_DIR} does not exist"
  exit 1
fi

cd "${APP_DIR}"

if [[ ! -d .git ]]; then
  echo "ERROR: ${APP_DIR} is not a git repository"
  exit 1
fi

echo "==> Fetch and pull"
git fetch --all --prune
git pull --ff-only

if [[ ! -d venv ]]; then
  echo "==> Create venv"
  "${PYTHON_BIN}" -m venv venv
fi

echo "==> Install dependencies"
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "==> Restart service"
sudo systemctl daemon-reload
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl enable "${SERVICE_NAME}"

echo "==> Local health checks"
curl -fsS "http://127.0.0.1:5000/health" >/dev/null
curl -fsS "http://127.0.0.1:5000/search?q=test" >/dev/null

echo "==> Done"
sudo systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,12p'
