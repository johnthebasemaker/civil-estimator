#!/usr/bin/env bash
# First-time server install for Civil Estimator on Ubuntu 24.04.
#
# Run as root on a fresh machine. It is safe to run again: every step checks
# before it acts, so a half-finished install can be resumed rather than undone.
#
#   bash deploy/install.sh estimator.example.com
#
# It does NOT obtain a TLS certificate or edit DNS — those need your domain and
# your account, and are the two steps in the guide that stay yours.
set -euo pipefail

DOMAIN="${1:-}"
APP_USER=estimator
APP_HOME=/opt/civil-estimator
DATA_DIR=/var/lib/civil-estimator
ETC_DIR=/etc/civil-estimator

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -n "$DOMAIN" ] || { echo "usage: bash deploy/install.sh <domain>" >&2; exit 1; }

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev build-essential git nginx curl

echo "==> user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_HOME" "$DATA_DIR/cache" "$ETC_DIR" /var/www/certbot
chown -R "$APP_USER:$APP_USER" "$APP_HOME" "$DATA_DIR"

echo "==> application"
if [ ! -d "$APP_HOME/.git" ]; then
  echo "    Copy or clone the repository into $APP_HOME, then run this again."
  echo "    e.g.  rsync -a --exclude venv --exclude output ./ root@$DOMAIN:$APP_HOME/"
  exit 1
fi
sudo -u "$APP_USER" python3 -m venv "$APP_HOME/venv" 2>/dev/null || true
sudo -u "$APP_USER" "$APP_HOME/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_HOME/venv/bin/pip" install -q -r "$APP_HOME/requirements.txt"

echo "==> configuration"
if [ ! -f "$ETC_DIR/civil-estimator.env" ]; then
  install -m 0640 -o root -g "$APP_USER" \
    "$APP_HOME/deploy/civil-estimator.env.example" "$ETC_DIR/civil-estimator.env"
  echo "    wrote $ETC_DIR/civil-estimator.env — review it"
fi

echo "==> ollama"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
systemctl enable --now ollama
sudo -u "$APP_USER" ollama pull qwen2.5vl:7b || \
  ollama pull qwen2.5vl:7b

echo "==> services"
install -m 0644 "$APP_HOME/deploy/civil-estimator-web.service" /etc/systemd/system/
install -m 0644 "$APP_HOME/deploy/civil-estimator-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now civil-estimator-web civil-estimator-worker

echo "==> nginx"
sed "s/estimator.example.com/$DOMAIN/g" \
  "$APP_HOME/deploy/nginx-civil-estimator.conf" > /etc/nginx/sites-available/civil-estimator
ln -sf /etc/nginx/sites-available/civil-estimator /etc/nginx/sites-enabled/civil-estimator
rm -f /etc/nginx/sites-enabled/default

cat <<NEXT

Installed. Two steps remain, and both are yours:

  1. Point $DOMAIN at this server's IP address.
  2. Get a certificate, which also reloads nginx:

       apt-get install -y certbot python3-certbot-nginx
       certbot --nginx -d $DOMAIN

  Then set the app password:

       sudo -u $APP_USER $APP_HOME/venv/bin/python bin/set_password.py

  Check it is alive:

       systemctl status civil-estimator-web civil-estimator-worker
       journalctl -u civil-estimator-worker -f

NEXT
