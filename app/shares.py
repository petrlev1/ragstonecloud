"""Публичные ссылки «Облака»: расшаривание файлов и папок по ссылке (PostgreSQL).

Таблица shares(token PK, rel, created):
  - token — случайный, не угадать (secrets.token_urlsafe(16));
  - rel — путь от корня хранилища, всегда в '/'‑нотации без ведущего слэша
    (на Windows storage.rel_of даёт '\\' — нормализуем при записи).

Ссылка живёт, пока жив объект: переименование/перенос обновляют rel
(хуки rename_path из main.py), удаление снимает ссылки (delete_path).
"""
import os
import secrets

import psycopg
from fastapi import HTTPException

DB = None            # параметры подключения (config.json -> db)
ENABLED = False      # False, если БД не настроена


def log(*args):
    print("shares:", *args, flush=True)


def _connect():
    return psycopg.connect(host=DB["host"], port=DB["port"], dbname=DB["db"],
                           user=DB["user"], password=DB["password"])


def norm(rel: str) -> str:
    """rel-путь в единой '/'‑нотации без ведущего слэша."""
    return (rel or "").replace("\\", "/").strip("/")


def init(cfg: dict):
    """Создаёт таблицу при старте приложения (как indexer.init)."""
    global DB, ENABLED
    DB = cfg.get("db")
    if not DB:
        log("БД не настроена — расшаривание выключено")
        return
    ENABLED = True
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shares(
                token   TEXT PRIMARY KEY,
                rel     TEXT NOT NULL,
                created TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS shares_rel_idx ON shares(rel)")
    log("схема готова")


def create(rel: str) -> dict:
    """Создаёт ссылку; возвращает {'token', 'rel', 'created'}."""
    if not ENABLED:
        raise HTTPException(503, "Расшаривание выключено: нет db в config.json")
    rel = norm(rel)
    token = secrets.token_urlsafe(16)
    with _connect() as conn:
        conn.execute("INSERT INTO shares(token, rel) VALUES (%s, %s)",
                     (token, rel))
    return get(token)


def get(token: str) -> dict:
    """Одна ссылка по токену; 404, если такой нет."""
    if not ENABLED:
        raise HTTPException(404, "Ссылка не найдена")
    with _connect() as conn:
        row = conn.execute(
            "SELECT rel, extract(epoch FROM created)::int "
            "FROM shares WHERE token = %s", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "Ссылка не найдена")
    return {"token": token, "rel": row[0], "created": row[1]}


def rel_of(token: str) -> str:
    """rel-путь по токену (для публичных эндпоинтов); 404, если нет."""
    return get(token)["rel"]


def list_by_rel(rel: str) -> list[dict]:
    """Все ссылки на объект (для диалога у строки), новые сверху."""
    if not ENABLED:
        return []
    rel = norm(rel)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token, rel, extract(epoch FROM created)::int "
            "FROM shares WHERE rel = %s ORDER BY created DESC", (rel,)
        ).fetchall()
    return [{"token": t, "rel": r, "created": c} for t, r, c in rows]


def all_() -> list[dict]:
    """Все активные ссылки (для раздела «Мои ссылки»), новые сверху."""
    if not ENABLED:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token, rel, extract(epoch FROM created)::int "
            "FROM shares ORDER BY created DESC").fetchall()
    return [{"token": t, "rel": r, "created": c} for t, r, c in rows]


def remove(token: str) -> bool:
    """Отзывает ссылку. True, если она существовала."""
    if not ENABLED:
        return False
    with _connect() as conn:
        cur = conn.execute("DELETE FROM shares WHERE token = %s", (token,))
    return cur.rowcount > 0


def rename_path(old_rel: str, new_rel: str) -> None:
    """Объект (или папка со всем содержимым) переехал: едем за ним."""
    if not ENABLED or not old_rel:
        return
    old = norm(old_rel)
    new = norm(new_rel)
    if old == new:
        return
    with _connect() as conn:
        conn.execute(
            "UPDATE shares SET rel = %s || substr(rel, %s) "
            "WHERE rel = %s OR rel LIKE %s",
            (new, len(old) + 1, old, old + "/%"))


def delete_path(rel: str) -> None:
    """Объект удалён: снимаем ссылку на него и на всё вложенное."""
    if not ENABLED:
        return
    rel = norm(rel)
    if not rel:
        return  # корень не удаляется
    with _connect() as conn:
        conn.execute("DELETE FROM shares WHERE rel = %s OR rel LIKE %s",
                     (rel, rel + "/%"))
