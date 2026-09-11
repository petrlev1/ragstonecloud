#!/usr/bin/env python3
"""Создание/обновление config.json «Облака».

Примеры:
  python app/setup.py                        # спросит логин и пароль
  python app/setup.py --user '+79779057956'  # сменить логин (пароль — только если задан)
  python app/setup.py --password '…'         # сменить пароль
  python app/setup.py --root /srv/cloud      # другой корень хранилища (по умолчанию ~)
"""
import argparse
import getpass
import json
import os
import secrets
import sys

import auth
import config


def main() -> None:
    ap = argparse.ArgumentParser(description="Настройка config.json для «Облака»")
    ap.add_argument("--user", help="логин для входа (иначе: CLOUD_USER или ввод с клавиатуры)")
    ap.add_argument("--password", help="пароль для входа (иначе: CLOUD_PASSWORD или ввод с клавиатуры)")
    ap.add_argument("--root", help="корень хранилища (по умолчанию домашний каталог)")
    a = ap.parse_args()

    cfg = {}
    if os.path.exists(config.PATH):
        with open(config.PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    cur_user = (cfg.get("user") or "").strip()
    # спрашиваем только в интерактиве: есть tty и не выставлен CLOUD_NO_PROMPT
    ask = sys.stdin.isatty() and not os.environ.get("CLOUD_NO_PROMPT")

    # логин — не секрет, можно и аргументом
    user = (a.user or os.environ.get("CLOUD_USER") or "").strip()
    if not user:
        if ask:
            try:
                user = input(f"Логин [{cur_user}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                user = ""
        user = user or cur_user
    if not user:
        sys.exit("Логин не задан (--user или CLOUD_USER)")
    cfg["user"] = user

    # пароль — только хеш; пустой ввод = оставить текущий
    pw = a.password or os.environ.get("CLOUD_PASSWORD")
    if pw is None:
        if ask:
            try:
                pw = getpass.getpass("Пароль для входа (Enter — оставить текущий): ")
                if pw and getpass.getpass("Ещё раз: ") != pw:
                    sys.exit("Пароли не совпадают")
            except (EOFError, KeyboardInterrupt):
                sys.exit("Ввод прерван — пароль не изменён")
        elif not cfg.get("password"):
            sys.exit("Пароль не задан (--password или CLOUD_PASSWORD)")
    if pw:
        if len(pw) < 6:
            sys.exit("Пароль слишком короткий (нужно минимум 6 символов)")
        cfg["password"] = auth.make_hash(pw)

    cfg.setdefault("secret", secrets.token_hex(32))
    if a.root:
        cfg["root"] = os.path.abspath(os.path.expanduser(a.root))

    with open(config.PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(config.PATH, 0o600)
    print(f"config.json готов. Логин: {cfg['user']}")
    print(f"Корень хранилища: {cfg.get('root')}")
    print(f"Файл: {config.PATH} (права 600, вне git)")


if __name__ == "__main__":
    main()
