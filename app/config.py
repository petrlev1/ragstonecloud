"""Конфигурация «Облака». Читает config.json из корня проекта (вне git)."""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "config.json")

DEFAULTS = {
    "root": os.path.expanduser("~"),          # корень хранилища
    "session_days": 30,                        # жизнь сессии, дней
    "hide_dot": True,                          # скрывать .-файлы/.-папки из листинга
    "secret": None,                            # секрет подписи сессий (генерит setup.py)
    "password": None,                          # {"salt", "hash", "iterations"} — PBKDF2
    "rg": os.path.join(os.path.expanduser("~"), "bin", "rg"),  # путь к ripgrep
}


def load() -> dict:
    if not os.path.exists(PATH):
        raise SystemExit(
            "config.json не найден. Первый запуск: ./run.sh "
            "(создаст конфиг и спросит пароль) или python app/setup.py")
    with open(PATH, encoding="utf-8") as f:
        cfg = {**DEFAULTS, **json.load(f)}
    cfg["root"] = os.path.realpath(cfg["root"])
    if not cfg.get("secret") or not cfg.get("password"):
        raise SystemExit("config.json неполный — пересоздай: python app/setup.py")
    return cfg
