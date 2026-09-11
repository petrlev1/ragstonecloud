"""Безопасная работа с файловой системой: всё строго внутри корня хранилища."""
import os
import re

from fastapi import HTTPException

ROOT = ""  # задаётся из config.json при старте


def safe(rel: str) -> str:
    """rel-путь -> абсолютный. Запрещает выход за пределы ROOT (в т.ч. через symlink)."""
    rel = (rel or "").strip("/")
    if not rel:
        return ROOT
    p = os.path.realpath(os.path.join(ROOT, *rel.split("/")))
    if not is_within(p):
        raise HTTPException(403, "Путь за пределами хранилища")
    return p


def is_within(abs_path: str) -> bool:
    p = os.path.realpath(abs_path)
    root = os.path.realpath(ROOT)
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def rel_of(abs_path: str) -> str:
    r = os.path.relpath(os.path.realpath(abs_path), os.path.realpath(ROOT))
    return "" if r == "." else r


def list_dir(abs_dir: str, hide_dot: bool = True) -> list[dict]:
    try:
        names = os.listdir(abs_dir)
    except PermissionError:
        raise HTTPException(403, "Нет доступа к папке")
    except NotADirectoryError:
        raise HTTPException(400, "Это не папка")

    def sort_key(name: str):
        full = os.path.join(abs_dir, name)
        return (not os.path.isdir(full), name.lower())

    out = []
    for name in sorted(names, key=sort_key):
        if hide_dot and name.startswith("."):
            continue
        full = os.path.join(abs_dir, name)
        try:
            st = os.stat(full)  # следует за symlink (наружу — отсечётся в safe())
            is_dir = os.path.isdir(full)
        except OSError:
            continue  # битая ссылка/нет доступа — пропускаем
        out.append({
            "name": name,
            "type": "dir" if is_dir else "file",
            "size": None if is_dir else st.st_size,
            "mtime": int(st.st_mtime),
            "rel": rel_of(full),
        })
    return out


_NAME_BAD = re.compile(r"[/\\\x00]")

def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Пустое имя")
    if name in (".", "..") or _NAME_BAD.search(name):
        raise HTTPException(400, "Недопустимое имя")
    if len(name) > 200:
        raise HTTPException(400, "Имя слишком длинное")
    return name


def unique_name(dir_path: str, name: str) -> str:
    """Если имя занято — добавляет « (1)», « (2)»… (как Google Drive)."""
    if not os.path.exists(os.path.join(dir_path, name)):
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(dir_path, f"{stem} ({i}){ext}")):
        i += 1
    return f"{stem} ({i}){ext}"
