#!/usr/bin/env python3
"""Создание/обновление config.json «Облака».

Примеры:
  python app/setup.py                  # спросит пароль (или возьмёт CLOUD_PASSWORD)
  python app/setup.py --password '…'   # задать/сменить пароль
  python app/setup.py --root /srv/cloud# другой корень хранилища (по умолчанию ~)
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
    ap.add_argument("--password", help="пароль для входа (иначе: CLOUD_PASSWORD или ввод с клавиатуры)")
    ap.add_argument("--root", help="корень хранилища (по умолчанию домашний каталог)")
    a = ap.parse_args()

    pw = a.password or os.environ.get("CLOUD_PASSWORD")
    if pw is None:
        pw = getpass.getpass("Пароль для входа в облако: ")
        if getpass.getpass("Ещё раз: ") != pw:
            sys.exit("Пароли не совпадают")
    if len(pw) < 6:
        sys.exit("Пароль слишком короткий (нужно минимум 6 символов)")

    cfg = {}
    if os.path.exists(config.PATH):
        with open(config.PATH, encoding="utf-8") as f:
            cfg = json.load(f)

    cfg.setdefault("secret", secrets.token_hex(32))
    if a.root:
        cfg["root"] = os.path.abspath(os.path.expanduser(a.root))
    cfg["password"] = auth.make_hash(pw)

    with open(config.PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(config.PATH, 0o600)
    print(f"config.json готов. Корень хранилища: {cfg.get('root')}")
    print(f"Файл: {config.PATH} (права 600, вне git)")


if __name__ == "__main__":
    main()
