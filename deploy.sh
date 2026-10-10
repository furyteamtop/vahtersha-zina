#!/bin/bash
# Ставит Зину на сервер (Ubuntu/Debian) как службу, которая работает круглосуточно
# и сама перезапускается после сбоев и перезагрузок сервера.
#
#   ./deploy.sh root@IP_СЕРВЕРА
#
# Повторный запуск обновляет код бота на сервере.
set -euo pipefail
HOST="${1:?Укажите сервер: ./deploy.sh root@IP_СЕРВЕРА}"
cd "$(dirname "$0")"
[ -f .env ] || { echo "Нет файла .env — сначала запустите ./set_token.sh"; exit 1; }

echo "→ Копирую бота на $HOST ..."
COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -czf - zina requirements.txt .env | ssh "$HOST" 'mkdir -p /tmp/zina-upload && tar xzf - -C /tmp/zina-upload'

echo "→ Настраиваю сервер ..."
ssh "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
SUDO=""; [ "$(id -u)" = 0 ] || SUDO=sudo
DIR=/opt/zina

if ! python3 -c "import ensurepip, venv" 2>/dev/null && command -v apt-get >/dev/null; then
  # NEEDRESTART_SUSPEND — чтобы установка пакета не перезапустила другие службы на сервере
  APT="$SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get"
  $APT install -y -qq python3-venv >/dev/null || { $APT update -qq && $APT install -y -qq python3-venv >/dev/null; }
fi

id zina >/dev/null 2>&1 || $SUDO useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin zina
$SUDO mkdir -p "$DIR/data"
$SUDO rm -rf "$DIR/zina"
$SUDO cp -r /tmp/zina-upload/. "$DIR/"
rm -rf /tmp/zina-upload
[ -x "$DIR/.venv/bin/python" ] || $SUDO python3 -m venv "$DIR/.venv"
$SUDO "$DIR/.venv/bin/pip" install -q --upgrade pip
$SUDO "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
$SUDO chown -R zina:zina "$DIR"
$SUDO chmod 600 "$DIR/.env"

$SUDO tee /etc/systemd/system/zina.service >/dev/null <<UNIT
[Unit]
Description=Vahtersha Zina Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=zina
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m zina
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$DIR/data

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable zina >/dev/null 2>&1
$SUDO systemctl restart zina
sleep 4
$SUDO systemctl --no-pager --lines=5 status zina | tail -6
REMOTE

echo
echo "✅ Готово. Зина работает на сервере."
echo "   Не забудьте остановить бота на Mac (Ctrl+C в его вкладке терминала):"
echo "   две копии с одним токеном мешают друг другу."
echo "   Логи на сервере:  ssh $HOST journalctl -u zina -f"
