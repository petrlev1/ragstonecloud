"""Просмотр: миниатюры изображений (Pillow + дисковый кэш) и чтение текстов.

Кэш миниатюр лежит вне хранилища (по умолчанию `.thumbcache/` в каталоге проекта,
ключ `thumb_cache` в config.json). Pillow — необязательная зависимость: без неё
миниатюры не отдаются (UI покажет полную картинку), остальное работает как есть.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

from app import config as cfg_mod

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except Exception:            # Pillow не установлен — деградация без падения
    HAVE_PIL = False

# типы, которые галерея/просмотрщик умеют показывать (клиент использует те же наборы)
IMG_EXTS = {"jpg", "jpeg", "jfif", "png", "gif", "webp", "bmp", "tif", "tiff", "ico", "avif"}

DEFAULT_W = 240              # ширина миниатюры по умолчанию
MAX_W = 1024                 # потолок (кэш не должен пухнуть от «полных превью»)
CACHE_LIMIT = 40000          # файлов в кэше; при переполнении удаляются самые старые
MAX_TEXT_BYTES = 2 * 1024 * 1024   # сколько читать из текстового файла в предпросмотр
MAX_TEXT_FILE = 8 * 1024 * 1024    # больше этого — вообще отказываем

CACHE_DIR = ""
_lock = threading.Lock()
_last_prune = 0.0


def init(cfg: dict) -> None:
    """Каталог кэша миниатюр (создаётся при старте)."""
    global CACHE_DIR
    CACHE_DIR = cfg.get("thumb_cache") or os.path.join(cfg_mod.BASE, ".thumbcache")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError:
        CACHE_DIR = ""


def ext_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lstrip(".").lower()


def is_image(name: str) -> bool:
    return ext_of(name) in IMG_EXTS


def _cache_path(abs_path: str, w: int) -> str:
    st = os.stat(abs_path)
    key = hashlib.sha1(
        f"{os.path.realpath(abs_path)}|{st.st_mtime_ns}|{st.st_size}|{w}".encode("utf-8")
    ).hexdigest()
    return os.path.join(CACHE_DIR, key + ".jpg")


def _prune() -> None:
    """Редкая уборка: если кэш разросся — снести самые старые 20 %."""
    global _last_prune
    now = time.time()
    if now - _last_prune < 300:
        return
    _last_prune = now
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    if len(names) <= CACHE_LIMIT:
        return
    files = []
    for n in names:
        p = os.path.join(CACHE_DIR, n)
        try:
            files.append((os.path.getmtime(p), p))
        except OSError:
            continue
    files.sort()
    for _, p in files[: int(len(files) * 0.2)]:
        try:
            os.remove(p)
        except OSError:
            pass


def thumb(abs_path: str, w: int = DEFAULT_W):
    """Путь к jpeg-миниатюре файла (или None: не картинка / Pillow нет / ошибка чтения)."""
    if not HAVE_PIL or not CACHE_DIR:
        return None
    try:
        w = max(32, min(int(w), MAX_W))
    except (TypeError, ValueError):
        w = DEFAULT_W
    try:
        out = _cache_path(abs_path, w)
    except OSError:
        return None
    if os.path.exists(out):
        return out

    tmp = f"{out}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with Image.open(abs_path) as im:
            im = ImageOps.exif_transpose(im)     # поворот по EXIF — как в проводнике
            im.thumbnail((w, w), Image.LANCZOS)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.save(tmp, "JPEG", quality=82, optimize=True)
        os.replace(tmp, out)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    _prune()
    return out


def read_text(abs_path: str) -> dict:
    """Текст файла для предпросмотра: utf-8, при неудаче cp1251, длинное обрезается."""
    st = os.stat(abs_path)
    if st.st_size > MAX_TEXT_FILE:
        raise ValueError("Файл слишком большой для предпросмотра")
    with open(abs_path, "rb") as fh:
        raw = fh.read(MAX_TEXT_BYTES + 1)
    truncated = len(raw) > MAX_TEXT_BYTES
    raw = raw[:MAX_TEXT_BYTES]
    enc = "utf-8"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1251")
            enc = "cp1251"
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
            enc = "utf-8 (некоторые байты заменены)"
    return {"text": text.replace("\x00", ""), "truncated": truncated,
            "encoding": enc, "size": st.st_size}
