#!/bin/bash
# Спрашивает токен бота, записывает его в .env и запускает бота.
cd "$(dirname "$0")" || exit 1
[ -f .env ] || cp .env.example .env

printf "Вставьте токен от @BotFather и нажмите Enter (символы не отображаются): "
read -rs token
echo
# вытаскиваем сам токен, отбрасывая пробелы и служебные символы, которые терминал добавляет при вставке
token="$(printf '%s' "$token" | grep -oE '[0-9]{6,}:[A-Za-z0-9_-]{30,}' | head -1)"

if [ -z "$token" ]; then
  echo "Это не похоже на токен. Он выглядит так: 1234567890:AAH... Попробуйте ещё раз."
  exit 1
fi

grep -v '^BOT_TOKEN=' .env > .env.tmp
{ echo "BOT_TOKEN=$token"; cat .env.tmp; } > .env
rm -f .env.tmp
chmod 600 .env
echo "Токен сохранён. Запускаю Зину (остановить: Ctrl+C)..."
exec .venv/bin/python -m zina
