#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

SRC="${1:-/tmp/newsradar-src}"
APP_DIR="/opt/newsradar"
DATA_DIR="/var/lib/newsradar"
BACKUP_DIR="/var/backups/newsradar"
ETC_DIR="/etc/newsradar"
VENV="${APP_DIR}/.venv"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip sqlite3 ca-certificates tzdata curl caddy

if ! id newsradar >/dev/null 2>&1; then
  useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin newsradar
fi

install -d -m 755 -o newsradar -g newsradar "${APP_DIR}" "${DATA_DIR}" "${BACKUP_DIR}"
install -d -m 750 -o newsradar -g newsradar "${ETC_DIR}"

rsync -a --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='var' \
  "${SRC}/" "${APP_DIR}/"
chown -R newsradar:newsradar "${APP_DIR}"

if [[ ! -x "${VENV}/bin/python" ]]; then
  runuser -u newsradar -- python3 -m venv "${VENV}"
fi
# Older editable installs can leave a finder/.pth that pip 25 tries to stat
# from an inaccessible cwd.  Remove them before recreating the editable install.
rm -f "${VENV}"/lib/python*/site-packages/__editable__*newsradar* 2>/dev/null || true
runuser -u newsradar -- "${VENV}/bin/pip" install --no-cache-dir --upgrade pip
runuser -u newsradar -- bash -lc "cd /tmp && '${VENV}/bin/pip' install --no-cache-dir -e '${APP_DIR}'"

if [[ ! -s "${ETC_DIR}/newsradar.env" ]]; then
  cat >"${ETC_DIR}/newsradar.env" <<'EOF'
NR_ENV=production
NR_DB_PATH=/var/lib/newsradar/newsradar.db
NR_MASTER_KEY_FILE=/etc/newsradar/master.key
NR_HOST=127.0.0.1
NR_PORT=8080
NR_LOG_LEVEL=INFO
EOF
  chmod 600 "${ETC_DIR}/newsradar.env"
  chown newsradar:newsradar "${ETC_DIR}/newsradar.env"
fi

if [[ ! -s "${ETC_DIR}/master.key" ]]; then
  python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())' >"${ETC_DIR}/master.key"
  chmod 600 "${ETC_DIR}/master.key"
  chown newsradar:newsradar "${ETC_DIR}/master.key"
fi

set -a
# shellcheck disable=SC1091
source "${ETC_DIR}/newsradar.env"
set +a
runuser -u newsradar -- bash -lc "cd '${APP_DIR}' && NR_DB_PATH='${NR_DB_PATH}' NR_MASTER_KEY_FILE='${NR_MASTER_KEY_FILE}' '${VENV}/bin/alembic' upgrade head"
chmod 600 "${NR_DB_PATH}" 2>/dev/null || true
chmod 600 "${NR_DB_PATH}-wal" "${NR_DB_PATH}-shm" 2>/dev/null || true

install -m 644 "${APP_DIR}/deploy/systemd/newsradar-web.service" /etc/systemd/system/newsradar-web.service
install -m 644 "${APP_DIR}/deploy/systemd/newsradar-worker.service" /etc/systemd/system/newsradar-worker.service
install -m 644 "${APP_DIR}/deploy/Caddyfile" /etc/caddy/Caddyfile

caddy validate --config /etc/caddy/Caddyfile >/dev/null

systemctl daemon-reload
systemctl enable --now caddy
systemctl enable --now newsradar-web newsradar-worker
systemctl restart newsradar-web newsradar-worker
systemctl restart caddy

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp || true
  ufw allow 443/tcp || true
fi

sleep 3
echo "--- services ---"
systemctl --no-pager --full status newsradar-web newsradar-worker caddy | sed -n '1,80p' || true
echo "--- health ---"
curl -fsS http://127.0.0.1:8080/healthz || true
echo
echo "install complete; open http://$(hostname -I | awk '{print $1}')/"
