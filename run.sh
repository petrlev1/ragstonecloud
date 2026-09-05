#!/usr/bin/env bash
# «Облако» — запуск. Первый запуск: создаст venv и спросит пароль (config.json).
# Порт/хост можно переопределить: PORT=8090 ./run.sh
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f config.json ]; then
  echo "Первый запуск — придумай пароль для входа в облако:"
  ./venv/bin/python app/setup.py
fi

# Облако слушает ТОЛЬКО localhost: наружу его отдаёт Caddy (HTTPS).
# Если нужно публично без Caddy: HOST=0.0.0.0 ./run.sh
exec ./venv/bin/python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}"
