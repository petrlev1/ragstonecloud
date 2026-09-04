"""Полнотекстовый индекс «Облака» в PostgreSQL (БД cloud).

Таблица files(path, name, mtime, size, content, tsv):
  - content — извлечённый текст (NULL для бинарных файлов);
  - tsv — tsvector для поиска по содержимому (русская морфология);
  - name — имя файла, поиск по нему через pg_trgm (подстроки).
Удаление строки файла = удаление из индекса; место освобождает autovacuum.
"""
import os
import re
import threading
import time
import traceback

import psycopg

TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".css",
    ".js", ".mjs", ".ts", ".py", ".sh", ".bat", ".cmd", ".ps1", ".log",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sql", ".c", ".cpp",
    ".h", ".hpp", ".java", ".go", ".rs", ".php", ".rb", ".pl", ".lua", ".r",
    ".tex", ".properties", ".ipynb", ".svg",
}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
SKIP_DIRS = {"venv", "node_modules", "__pycache__", ".git"}
MAX_FILE = 50 * 1024 * 1024   # файлы больше не индексируем (как rg --max-filesize)
MAX_TEXT = 2_000_000          # потолок извлекаемого текста на файл (символов)

DB = None          # dict с параметрами подключения
ROOT = ""          # корень хранилища
ENABLED = False    # False, если БД не настроена в config.json

_lock = threading.Lock()
_status = {"running": False, "phase": "idle", "done": 0, "total": 0,
           "last_sync": None, "errors": 0, "last_error": None}


def log(*args):
    print("indexer:", *args, flush=True)


# ---------- подключение / схема ----------

def _connect(autocommit=False):
    return psycopg.connect(host=DB["host"], port=DB["port"], dbname=DB["db"],
                           user=DB["user"], password=DB["password"],
                           autocommit=autocommit)


def init(cfg: dict):
    """Вызывается при старте приложения."""
    global DB, ROOT, ENABLED
    DB = cfg.get("db")
    ROOT = cfg["root"]
    if not DB:
        log("БД не настроена — индекс выключен")
        return
    ENABLED = True
    with _connect() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files(
                path    TEXT PRIMARY KEY,
                name    TEXT NOT NULL,
                mtime   BIGINT NOT NULL,
                size    BIGINT,
                content TEXT,
                tsv     TSVECTOR
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS files_tsv_idx ON files USING GIN(tsv)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS files_name_trgm "
            "ON files USING GIN(name gin_trgm_ops)")
    log(f"схема готова, root={ROOT}")
    sync_start(background=True)


# ---------- извлечение текста ----------

def extract_text(abs_path: str) -> str | None:
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext in PDF_EXTS:
            from pypdf import PdfReader
            reader = PdfReader(abs_path)
            parts = []
            for page in list(reader.pages)[:250]:
                t = page.extract_text() or ""
                if t:
                    parts.append(t)
            return " ".join(parts)[:MAX_TEXT] or None
        if ext in DOCX_EXTS:
            import docx  # python-docx
            d = docx.Document(abs_path)
            text = "\n".join(p.text for p in d.paragraphs)
            return text[:MAX_TEXT] or None
        if ext in TEXT_EXTS or ext == "":
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read(MAX_TEXT + 1)[:MAX_TEXT]
    except Exception:
        return None
    return None


def _is_skipped_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS


# ---------- точечные операции (вызываются из API при изменениях) ----------

def _upsert(conn, rel: str, mtime: int, size: int, content: str | None):
    # PG запрещает NUL-байты, а tsvector ограничен 1 МБ — вычищаем и урезаем
    if content:
        content = content.replace("\x00", "") or None
    if content and len(content) > 400_000:
        content = content[:400_000]
    conn.execute(
        """INSERT INTO files(path, name, mtime, size, content, tsv)
           VALUES(%s, %s, %s, %s, %s, to_tsvector('russian', %s))
           ON CONFLICT (path) DO UPDATE SET name=EXCLUDED.name,
             mtime=EXCLUDED.mtime, size=EXCLUDED.size,
             content=EXCLUDED.content, tsv=EXCLUDED.tsv""",
        (rel, os.path.basename(rel), mtime, size, content, content))


def index_path(rel: str):
    """Проиндексировать один файл по rel-пути (после загрузки/изменения)."""
    if not ENABLED:
        return
    abs_path = os.path.join(ROOT, rel) if rel else ROOT
    try:
        st = os.stat(abs_path)
        if not os.path.isfile(abs_path) or st.st_size > MAX_FILE:
            return
        content = extract_text(abs_path)
        with _connect() as conn:
            _upsert(conn, rel, int(st.st_mtime), st.st_size, content)
    except OSError:
        pass


def delete_path(rel: str):
    """Удалить из индекса файл или целую папку (после удаления в облаке)."""
    if not ENABLED or not rel:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM files WHERE path = %s OR path LIKE %s",
                     (rel, rel + "/%"))


def rename_path(old_rel: str, new_rel: str):
    """Переименовать/перенести в индексе (файл или папку)."""
    if not ENABLED or not old_rel:
        return
    with _connect() as conn:
        conn.execute("UPDATE files SET name = %s WHERE path = %s",
                     (os.path.basename(new_rel), old_rel))
        conn.execute(
            "UPDATE files SET path = %s || substr(path, length(%s) + 1) "
            "WHERE path = %s OR path LIKE %s",
            (new_rel, old_rel, old_rel, old_rel + "/%"))


# ---------- полная синхронизация (фоновый поток) ----------

def status() -> dict:
    with _lock:
        return dict(_status)


def count() -> int:
    if not ENABLED:
        return 0
    try:
        with _connect() as conn:
            return conn.execute("SELECT count(*) FROM files").fetchone()[0]
    except Exception:
        return 0


def sync_start(background: bool = True) -> bool:
    """Запускает синхронизацию индекса с диском. True, если запущена сейчас."""
    if not ENABLED:
        return False
    with _lock:
        if _status["running"]:
            return False
        _status["running"] = True
        _status["phase"] = "walk"
        _status["done"] = 0
        _status["total"] = 0
        _status["errors"] = 0
        _status["last_error"] = None
    log("sync: запуск потока")
    t = threading.Thread(target=_sync_run, daemon=True)
    t.start()
    return True


def _sync_run():
    try:
        log(f"sync: обход диска, root={ROOT}")
        # 1) обход диска: rel -> (mtime, size)
        disk: dict[str, tuple[int, int]] = {}

        def _on_walk_error(e):
            log(f"sync: ошибка обхода: {e}")

        for root, dirs, files in os.walk(ROOT, onerror=_on_walk_error):
            dirs[:] = [d for d in dirs if not _is_skipped_dir(d)]
            for fn in files:
                if fn.startswith("."):
                    continue
                full = os.path.join(root, fn)
                try:
                    st = os.stat(full)
                    if not os.path.isfile(full):
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(full, ROOT)
                disk[rel] = (int(st.st_mtime), st.st_size)

        log(f"sync: обход завершён, на диске {len(disk)} файлов")
        with _connect() as conn:
            # 2) текущее состояние индекса
            db_rows = conn.execute(
                "SELECT path, mtime, size FROM files").fetchall()
            db_state = {p: (m, s) for p, m, s in db_rows}

            to_delete = [p for p in db_state if p not in disk]
            to_upsert = [p for p, ms in disk.items() if db_state.get(p) != ms]
            log(f"sync: новых/изменённых {len(to_upsert)}, удалённых {len(to_delete)}")

            # 3) удаления (освобождение места — autovacuum)
            for rel in to_delete:
                conn.execute(
                    "DELETE FROM files WHERE path = %s OR path LIKE %s",
                    (rel, rel + "/%"))
            if to_delete:
                log(f"sync: удалено из индекса {len(to_delete)}")
            conn.commit()

            # 4) новые/изменённые: извлечение + запись пакетами
            _status["phase"] = "index"
            _status["total"] = len(to_upsert)
            done = 0
            for i in range(0, len(to_upsert), 100):
                batch = to_upsert[i:i + 100]
                for rel in batch:
                    try:
                        full = os.path.join(ROOT, rel)
                        st = os.stat(full)
                        if st.st_size <= MAX_FILE:
                            content = extract_text(full)
                        else:
                            content = None
                        _upsert(conn, rel, int(st.st_mtime), st.st_size, content)
                    except Exception as e:
                        log(f"sync: пропущен {rel!r}: {e}")
                    done += 1
                conn.commit()
                _status["done"] = done
                if done % 1000 == 0 or done == len(to_upsert):
                    log(f"sync: {done}/{len(to_upsert)}")
    except Exception:
        log("sync: ОШИБКА:")
        traceback.print_exc()
        with _lock:
            _status["errors"] = _status.get("errors", 0) + 1
            _status["last_error"] = traceback.format_exc()[-500:]
    finally:
        with _lock:
            _status["running"] = False
            _status["phase"] = "idle"
            _status["last_sync"] = int(time.time())
        log("sync: завершён")


# ---------- поиск ----------

def _like_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def search(q: str, scope_rel: str = "", limit: int = 300) -> list[dict]:
    """Поиск по содержимому (tsvector, русская морфология) и имени (подстрока)."""
    if not ENABLED:
        raise RuntimeError("Полнотекстовый индекс не настроен (нет db в config.json)")
    if not re.search(r"[0-9A-Za-zА-Яа-яЁё]", q):
        return []
    like = "%" + _like_escape(q) + "%"
    sql = """
        SELECT path, name, size, mtime,
               (name ILIKE %s) AS name_hit,
               ts_rank(tsv, plainto_tsquery('russian', %s)) AS rank
        FROM files
        WHERE (content IS NOT NULL AND tsv @@ plainto_tsquery('russian', %s))
           OR name ILIKE %s
    """
    params = [like, q, q, like]
    if scope_rel:
        sql += " AND (path = %s OR path LIKE %s)"
        params += [scope_rel, _like_escape(scope_rel) + "/%"]
    sql += """
        ORDER BY name_hit DESC, rank DESC NULLS LAST, path
        LIMIT %s
    """
    params.append(limit)
    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except psycopg.Error:
        return []  # пустой/мусорный запрос — просто ничего не нашли
    out = []
    for path, name, size, mtime, _hit, _rank in rows:
        out.append({"name": name, "rel": path, "type": "file",
                    "size": size, "mtime": int(mtime) if mtime else None})
    return out
