#!/usr/bin/env sh
# Installs a systemd *user* service that runs the APLOS daily scheduler and
# restarts it after crashes and reboots (Linux).
# Usage:  sh scripts/install_systemd_service.sh
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/aplos-scheduler.service" <<EOF
[Unit]
Description=APLOS Solar Panel Schedule Adjuster - daily scheduler
After=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m aplos scheduler
Restart=always
RestartSec=60

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now aplos-scheduler.service
echo "Installed. For start at boot without login run: loginctl enable-linger $USER"
echo "Alternative (cron):  @reboot cd $ROOT && .venv/bin/python -m aplos scheduler"
