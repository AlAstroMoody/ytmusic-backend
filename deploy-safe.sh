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

python_version() {
  "${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

venv_is_usable() {
  [[ -x venv/bin/python ]] && venv/bin/python -c 'import sys' >/dev/null 2>&1
}

install_venv_packages() {
  echo "==> Install python venv packages (apt)"
  sudo apt-get update -qq
  sudo apt-get install -y python3-venv python3-pip
  local py_ver
  py_ver="$(python_version)"
  sudo apt-get install -y "python${py_ver}-venv" 2>/dev/null || true
}

create_venv() {
  rm -rf venv
  "${PYTHON_BIN}" -m venv venv
}

ensure_venv() {
  if venv_is_usable; then
    echo "==> venv OK"
    return 0
  fi

  echo "==> Create venv"
  if create_venv 2>/dev/null; then
    return 0
  fi

  install_venv_packages
  create_venv
}

wait_for_health() {
  local url="$1"
  local attempts="${2:-15}"

  for i in $(seq 1 "${attempts}"); do
    if curl -fsS "${url}" >/dev/null; then
      echo "OK: ${url} (attempt ${i})"
      return 0
    fi
    sleep 1
  done

  echo "ERROR: health check failed: ${url}"
  sudo journalctl -u "${SERVICE_NAME}" -n 30 --no-pager || true
  return 1
}

echo "==> Fetch and pull"
git fetch --all --prune
git pull --ff-only

ensure_venv

echo "==> Install dependencies"
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "==> Restart service"
sudo systemctl daemon-reload
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl enable "${SERVICE_NAME}"

echo "==> Local health checks"
wait_for_health "http://127.0.0.1:5000/health"
wait_for_health "http://127.0.0.1:5000/search?q=test"

echo "==> Done"
sudo systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,12p'
