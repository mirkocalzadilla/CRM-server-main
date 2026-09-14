#!/usr/bin/env bash
# Instala/actualiza el deploy pull-based en el VPS. Correr como root (los units quedan
# en /etc/systemd/system; el servicio corre como user 'deploy'). Idempotente.
#   sudo /home/deploy/marketing-services/deploy/systemd/install.sh
set -euo pipefail

REPO_DIR="/home/deploy/marketing-services"
UNIT_SRC="${REPO_DIR}/deploy/systemd"

chmod +x "${REPO_DIR}/scripts/vps-pull-deploy.sh"
install -d -o deploy -g deploy /home/deploy/.mks-deploy

cp "${UNIT_SRC}/mks-deploy.service" /etc/systemd/system/mks-deploy.service
cp "${UNIT_SRC}/mks-deploy.timer"   /etc/systemd/system/mks-deploy.timer

systemctl daemon-reload
systemctl enable --now mks-deploy.timer

echo "Instalado. Estado:"
systemctl status mks-deploy.timer --no-pager || true
echo "Logs del servicio:  journalctl -u mks-deploy.service -f"
