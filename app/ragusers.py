"""Пользователи RAG (ragstone.ru) в «Облаке».

Что здесь есть:
  * проверка пароля RAG — хеши werkzeug (`scrypt:32768:8:1$salt$hash`,
    `pbkdf2:sha256:600000$salt$hash`) считаются на стандартной библиотеке,
    поэтому дополнительных зависимостей облаку не нужно;
  * вход по логину/паролю RAG (БД rag_system.users, роль только на SELECT);
  * личная папка пользователя внутри хранилища (`users_rel/<id>`) и квота места;
  * одноразовый SSO-токен для перехода «☁️ Мои файлы» из кабинета RAG.

Формат SSO-токена (одинаков на обеих сторонах — облако и RAG):
    <base64url(json{uid, u, exp, n})>.<hex hmac_sha256(secret, base64url-часть)>
  uid — id в rag_system.users, u — логин, exp — unix-время, n — одноразовый nonce.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.request

import psycopg

CFG: dict = {}
ROOT = ""
DB: dict | None = None
USERS_REL = "data/users"
RAG_DOCS = ""                       # шаблон папки документов ragstone (если задан — это «Мои файлы»)
RAG_API = ""                        # внутренний адрес RAG (переиндексация после загрузки из облака)
QUOTA = 0
SSO_SECRET: str | None = None
SSO_TTL = 120
NAMES: dict[int, str] = {}          # uid -> логин (кэш для подписи в шапке)
NONCES: dict[str, float] = {}       # уже использованные SSO-токены (одноразовость)


def log(*args):
    print("ragusers:", *args, flush=True)


def init(cfg: dict) -> None:
    global CFG, ROOT, DB, USERS_REL, RAG_DOCS, RAG_API, QUOTA, SSO_SECRET, SSO_TTL
    CFG = cfg
    ROOT = cfg["root"]
    DB = cfg.get("rag_db") or None
    USERS_REL = (cfg.get("users_rel") or "data/users").replace("\\", "/").strip("/")
    RAG_DOCS = (cfg.get("rag_docs_rel") or "").replace("\\", "/").strip("/")
    RAG_API = (cfg.get("rag_api") or "").strip()
    try:
        QUOTA = int(float(cfg.get("user_quota_gb") or 0) * 1024 ** 3)
    except (TypeError, ValueError):
        QUOTA = 0
    SSO_SECRET = cfg.get("sso_secret") or None
    SSO_TTL = int(cfg.get("sso_ttl") or 120)
    if not DB:
        log("БД пользователей RAG не настроена — вход только у владельца")
    if not SSO_SECRET:
        log("sso_secret не задан — переход «Мои файлы» из кабинета RAG недоступен")


def enabled() -> bool:
    return DB is not None


def quota() -> int:
    return QUOTA


# ---------- пароли RAG (совместимость с werkzeug, только stdlib) ----------

def check_password(password: str, stored: str) -> bool:
    """Сверка пароля с хешем werkzeug: scrypt/pbkdf2. False при любом сбое."""
    try:
        method, salt, digest = (stored or "").split("$", 2)
        parts = method.split(":")
        dklen = len(bytes.fromhex(digest))
        if parts[0] == "scrypt":
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            dk = hashlib.scrypt(password.encode(), salt=salt.encode(), n=n, r=r, p=p,
                                dklen=dklen, maxmem=132 * n * r * p * 2)
        elif parts[0] == "pbkdf2":
            dk = hashlib.pbkdf2_hmac(parts[1], password.encode(), salt.encode(),
                                     int(parts[2]), dklen=dklen)
        else:
            return False
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError, AttributeError, MemoryError):
        return False


def _connect():
    return psycopg.connect(host=DB["host"], port=DB["port"], dbname=DB["db"],
                           user=DB["user"], password=DB["password"], connect_timeout=5)


def authenticate(username: str, password: str) -> dict | None:
    """Проверка пары логин/пароль RAG. None — нет такого пользователя/неверный пароль."""
    if not DB:
        return None
    username = (username or "").strip()
    if not username or not password:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash FROM users "
                "WHERE lower(username) = lower(%s)", (username,)).fetchone()
    except Exception as exc:
        log("ошибка запроса к БД RAG:", type(exc).__name__, exc)
        return None
    if not row:
        return None
    uid, name, password_hash = row
    if not check_password(password, password_hash or ""):
        return None
    NAMES[int(uid)] = name
    ensure_dir(int(uid))
    return {"uid": int(uid), "username": name}


def name(uid: int) -> str:
    """Логин пользователя для подписи в интерфейсе (с кэшем)."""
    uid = int(uid)
    if uid in NAMES:
        return NAMES[uid]
    if not DB:
        return f"user_{uid}"
    try:
        with _connect() as conn:
            row = conn.execute("SELECT username FROM users WHERE id = %s", (uid,)).fetchone()
        if row:
            NAMES[uid] = row[0]
            return row[0]
    except Exception as exc:
        log("ошибка запроса к БД RAG:", type(exc).__name__, exc)
    return f"user_{uid}"


# ---------- личная папка и место ----------

def prefix_rel(uid: int) -> str:
    """rel-путь папки «Мои файлы»: документы ragstone (rag_docs_rel) либо data/users/<uid>."""
    if RAG_DOCS:
        try:
            return RAG_DOCS.format(uid=int(uid)).strip("/")
        except (KeyError, IndexError, ValueError):
            log("неверный шаблон rag_docs_rel:", RAG_DOCS)
    return f"{USERS_REL}/{int(uid)}"


def user_root(uid: int) -> str:
    return os.path.join(ROOT, *prefix_rel(uid).split("/"))


def ensure_dir(uid: int) -> str:
    p = user_root(uid)
    try:
        os.makedirs(p, mode=0o700, exist_ok=True)
    except OSError as exc:
        log("не удалось создать папку пользователя", uid, exc)
    return p


def dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda e: None):
        for fn in files:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


def usage(uid: int) -> int:
    p = user_root(uid)
    return dir_size(p) if os.path.isdir(p) else 0


def quota_info(uid: int) -> dict:
    used = usage(uid)
    return {"used": used, "limit": QUOTA, "free": max(0, QUOTA - used) if QUOTA else None}


def disk_info(uid: int) -> dict:
    """Описание «диска» пользователя для шапки UI: одна папка с квотой вместо дисков сервера."""
    info = quota_info(uid)
    return {"rel": prefix_rel(uid), "name": "Мои файлы", "type": None,
            "total": info["limit"] or None, "used": info["used"], "free": info["free"],
            "quota": True}


# ---------- связь с RAG: регистрация документов и переиндексация ----------

_UID_RE = None


def _uid_re():
    """Регулярка «папка БЗ пользователя → uid» из шаблона rag_docs_rel."""
    global _UID_RE
    if not RAG_DOCS:
        return None
    if _UID_RE is None:
        pat = re.escape(RAG_DOCS).replace(re.escape("{uid}"), r"(?P<uid>\d+)")
        _UID_RE = re.compile("^" + pat + r"(?:/|$)")
    return _UID_RE


def uid_of_rel(rel: str) -> int | None:
    """uid владельца папки БЗ по rel-пути (None — путь вне папок документов)."""
    rx = _uid_re()
    if not rx:
        return None
    m = rx.match((rel or "").replace("\\", "/").strip("/"))
    return int(m.group("uid")) if m else None


_NOTIFY_LOCK = threading.Lock()
_NOTIFY_LAST: dict[int, float] = {}


def notify_kb(uid: int | None, reason: str = "", throttle: float = 0) -> None:
    """Сообщить RAG, что в папке БЗ пользователя изменились файлы (фоном, без ожидания).

    RAG сверит папку с user_documents (новые / пропавшие / изменённые по размеру+mtime)
    и запустит переиндексацию базы знаний.
    throttle > 0 — не чаще одного вызова за указанное число секунд на пользователя
    (для «дёрнуть сверку при открытии папки»).
    Ошибка связи не влияет на операцию пользователя — только пишется в лог.
    """
    if not uid or not RAG_API or not SSO_SECRET:
        return
    if throttle:
        now = time.time()
        with _NOTIFY_LOCK:
            if now - _NOTIFY_LAST.get(int(uid), 0) < throttle:
                return
            _NOTIFY_LAST[int(uid)] = now

    def _worker():
        body = json.dumps({"uid": int(uid), "reason": reason},
                          ensure_ascii=False).encode()
        mac = hmac.new(SSO_SECRET.encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            RAG_API.rstrip("/") + "/api/internal/kb-sync", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Cloud-Signature": mac})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read() or b"{}")
            if data.get("added") or data.get("removed") or data.get("changed"):
                log(f"БЗ пользователя #{uid} обновлена ({reason}): "
                    f"+{data.get('added', 0)} / -{data.get('removed', 0)} / "
                    f"~{data.get('changed', 0)}")
        except Exception as exc:
            log(f"БЗ #{uid} ({reason}) не синхронизирована:", type(exc).__name__, exc)

    threading.Thread(target=_worker, daemon=True).start()


# ---------- SSO: переход «☁️ Мои файлы» из кабинета RAG ----------

def make_sso(uid: int, username: str, secret: str | None = None,
             ttl: int | None = None) -> str:
    """Токен для перехода в облако (используется стороной RAG и в тестах)."""
    secret = secret or SSO_SECRET
    if not secret:
        raise ValueError("sso_secret не задан")
    ttl = SSO_TTL if ttl is None else int(ttl)
    payload = {"uid": int(uid), "u": username or "", "exp": int(time.time()) + ttl,
               "n": secrets.token_urlsafe(12)}
    b64 = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")
    mac = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{mac}"


def check_sso(token: str) -> dict | None:
    """Проверка SSO-токена: подпись, срок, одноразовость. None — не годится."""
    if not SSO_SECRET or not token or "." not in token:
        return None
    try:
        b64, mac = token.split(".", 1)
        expected = hmac.new(SSO_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        data = json.loads(raw)
        exp = int(data.get("exp") or 0)
        if exp < time.time():
            return None
        nonce = str(data.get("n") or "")
        now = time.time()
        for k in [k for k, v in NONCES.items() if v < now]:
            NONCES.pop(k, None)
        if nonce:
            if nonce in NONCES:
                return None
            NONCES[nonce] = exp + 60
        uid = int(data.get("uid") or 0)
        if uid <= 0:
            return None
        if data.get("u"):
            NAMES[uid] = data["u"]
        ensure_dir(uid)
        return {"uid": uid, "username": data.get("u") or ""}
    except Exception:
        return None
