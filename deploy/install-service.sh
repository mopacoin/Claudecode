#!/bin/sh
# Richtet den Autostart (systemd) für DIESES Verzeichnis und den aufrufenden Benutzer ein.
# Aufruf aus dem Projektordner:  sudo sh deploy/install-service.sh
set -e
[ "$(id -u)" = 0 ] || exec sudo sh "$0" "$@"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-root}"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || { echo "Python-Umgebung fehlt: $PY – siehe README (python3 -m venv --system-site-packages .venv)"; exit 1; }
cat > /etc/systemd/system/growcontroller.service <<UNIT
[Unit]
Description=Growcontroller
# erst nach Netzwerk und (wenn verfügbar) Zeitabgleich starten – die Lichtzeiten hängen an der Uhr
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
EnvironmentFile=-$DIR/.env
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$PY -m growcontroller -c config.json --data data
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable growcontroller >/dev/null
systemctl restart growcontroller
echo "Autostart eingerichtet: $DIR (Benutzer $USER_NAME)"
TZ_NOW="$(timedatectl show -p Timezone --value 2>/dev/null || echo unbekannt)"
echo "Zeitzone: $TZ_NOW – für die Lichtzeiten muss sie stimmen (z. B.: sudo timedatectl set-timezone Europe/Berlin)"
systemctl --no-pager --lines=0 status growcontroller | head -3
