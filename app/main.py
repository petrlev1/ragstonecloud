"""«Облако» — личный веб-сервис файлов (аналог Google Drive).

Запуск: run.sh  (uvicorn app.main:app --host 0.0.0.0 --port 8080)
Конфигурация: config.json в корне проекта (вне git).
"""
import mimetypes
import os
import shutil
import tempfile
import time

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app import auth, config as cfg_mod, indexer, shares, storage

cfg = cfg_mod.load()
storage.ROOT = cfg["root"]
SECRET = cfg["secret"]
PW = cfg["password"]
SESSION_DAYS = int(cfg.get("session_days", 30))
HIDE_DOT = bool(cfg.get("hide_dot", True))
INDEX = os.path.join(cfg_mod.BASE, "static", "index.html")
SHARE_INDEX = os.path.join(cfg_mod.BASE, "static", "share.html")
LOGIN_COOKIE = "cl_session"
# лог неудачных входов для fail2ban (реальный IP клиента)
AUTH_LOG = os.path.join(cfg_mod.BASE, "auth_failures.log")

# полнотекстовый индекс (PostgreSQL): схема + фоновая синхронизация при старте
indexer.init(cfg)
# публичные ссылки (PostgreSQL): схема shares
shares.init(cfg)

app = FastAPI(title="Облако", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(cfg_mod.BASE, "static")), name="static")


# ---------- ошибки: единая форма {"error": "…"} ----------

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "Неверные данные запроса"}, status_code=400)


# ---------- авторизация ----------

def require_auth(request: Request):
    token = request.cookies.get(LOGIN_COOKIE)
    if not token or not auth.check_token(SECRET, token):
        raise HTTPException(401, "Требуется вход")


_login_attempts: dict[str, list[float]] = {}  # ip -> метки попыток (последняя минута)


def _client_ip(request: Request) -> str:
    """Реальный IP клиента: приложение слушает только localhost за Caddy,
    который всегда перезаписывает X-Forwarded-For адресом прямого клиента."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _log_auth_fail(ip: str) -> None:
    try:
        with open(AUTH_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" AUTHFAIL {ip}\n")
    except OSError:
        pass


def _rate_limited(ip: str) -> bool:
    now = time.time()
    lst = [t for t in _login_attempts.get(ip, []) if now - t < 60]
    _login_attempts[ip] = lst
    if len(lst) >= 5:
        return True
    lst.append(now)
    return False


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = _client_ip(request)
    if _rate_limited(ip):
        raise HTTPException(429, "Слишком много попыток — подожди минуту")
    if not auth.check_password(body.password, PW):
        _log_auth_fail(ip)
        raise HTTPException(401, "Неверный пароль")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(LOGIN_COOKIE, auth.make_token(SECRET, SESSION_DAYS),
                    max_age=SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", path="/")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(LOGIN_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(_=Depends(require_auth)):
    return {"ok": True}


# ---------- файлы ----------

class PathBody(BaseModel):
    path: str


class RenameBody(BaseModel):
    path: str
    name: str


class MoveBody(BaseModel):
    path: str
    dest: str


class MkdirBody(BaseModel):
    path: str
    name: str


@app.get("/")
def index():
    return FileResponse(INDEX, media_type="text/html")


@app.get("/api/list")
def api_list(path: str = "", _=Depends(require_auth)):
    d = storage.safe(path)
    if not os.path.isdir(d):
        raise HTTPException(404, "Папка не найдена")
    return {"path": storage.rel_of(d), "items": storage.list_dir(d, hide_dot=HIDE_DOT)}


@app.get("/api/file")
def api_file(path: str, _=Depends(require_auth)):
    p = storage.safe(path)
    if not os.path.isfile(p):
        raise HTTPException(404, "Файл не найден")
    media = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media, filename=os.path.basename(p),
                        content_disposition_type="inline")


def _zip_dir(abs_dir: str):
    """Папка -> временный zip. Возвращает (zpath, filename_zip, cleanup_fn)."""
    tmp = tempfile.mkdtemp(prefix="cloud_zip_")
    base = os.path.basename(abs_dir.rstrip(os.sep)) or "cloud"
    zpath = shutil.make_archive(os.path.join(tmp, base), "zip",
                                root_dir=os.path.dirname(abs_dir),
                                base_dir=os.path.basename(abs_dir))

    def _cleanup():
        try:
            os.remove(zpath)
            os.rmdir(tmp)
        except OSError:
            pass

    return zpath, f"{base}.zip", _cleanup


def _shares_hook(fn, *args):
    """Хуки shares (переименование/удаление) не должны ронять запрос."""
    try:
        fn(*args)
    except Exception as e:
        print("shares hook:", type(e).__name__, e, flush=True)


@app.get("/api/download")
def api_download(path: str, _=Depends(require_auth)):
    p = storage.safe(path)
    if os.path.isfile(p):
        media = mimetypes.guess_type(p)[0] or "application/octet-stream"
        return FileResponse(p, media_type=media, filename=os.path.basename(p),
                            content_disposition_type="attachment")
    if os.path.isdir(p):
        zpath, zname, cleanup = _zip_dir(p)
        return FileResponse(zpath, media_type="application/zip", filename=zname,
                            content_disposition_type="attachment",
                            background=BackgroundTask(cleanup))
    raise HTTPException(404, "Не найдено")


@app.post("/api/upload")
async def api_upload(path: str = "", files: list[UploadFile] = File(...),
                     _=Depends(require_auth)):
    d = storage.safe(path)
    if not os.path.isdir(d):
        raise HTTPException(404, "Папка не найдена")
    n = 0
    for up in files:
        name = os.path.basename((up.filename or "").replace("\\", "/"))
        if not name or name in (".", ".."):
            continue
        target = os.path.join(d, storage.unique_name(d, name))
        if not storage.is_within(target):
            continue  # защита от хитрых имён
        with open(target, "wb") as fh:
            while chunk := await up.read(1 << 20):
                fh.write(chunk)
        indexer.index_path(storage.rel_of(target))
        n += 1
    return {"ok": True, "uploaded": n}


@app.post("/api/folder")
def api_mkdir(body: MkdirBody, _=Depends(require_auth)):
    parent = storage.safe(body.path)
    name = storage.validate_name(body.name)
    if not os.path.isdir(parent):
        raise HTTPException(404, "Папка не найдена")
    target = os.path.join(parent, name)
    if os.path.exists(target):
        raise HTTPException(409, "Уже существует")
    os.mkdir(target)
    indexer.index_dir(storage.rel_of(target))
    return {"ok": True}


@app.post("/api/rename")
def api_rename(body: RenameBody, _=Depends(require_auth)):
    src = storage.safe(body.path)
    name = storage.validate_name(body.name)
    if not os.path.exists(src):
        raise HTTPException(404, "Не найдено")
    dst = os.path.join(os.path.dirname(src), name)
    if not storage.is_within(dst):
        raise HTTPException(403, "Недопустимое имя")
    if os.path.exists(dst):
        raise HTTPException(409, "Уже существует")
    old_rel = storage.rel_of(src)
    os.rename(src, dst)
    indexer.rename_path(old_rel, storage.rel_of(dst))
    _shares_hook(shares.rename_path, old_rel, storage.rel_of(dst))
    return {"ok": True}


@app.post("/api/move")
def api_move(body: MoveBody, _=Depends(require_auth)):
    src = storage.safe(body.path)
    dst_dir = storage.safe(body.dest)
    if not os.path.exists(src):
        raise HTTPException(404, "Не найдено")
    if not os.path.isdir(dst_dir):
        raise HTTPException(400, "Назначение — не папка")
    src_r = os.path.realpath(src)
    if os.path.realpath(dst_dir).startswith(src_r.rstrip(os.sep) + os.sep):
        raise HTTPException(400, "Нельзя переместить папку в саму себя")
    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dst):
        raise HTTPException(409, "В папке назначения уже есть такое имя")
    old_rel = storage.rel_of(src)
    shutil.move(src, dst)
    indexer.rename_path(old_rel, storage.rel_of(dst))
    _shares_hook(shares.rename_path, old_rel, storage.rel_of(dst))
    return {"ok": True}


@app.post("/api/delete")
def api_delete(body: PathBody, _=Depends(require_auth)):
    p = storage.safe(body.path)
    if p == storage.ROOT:
        raise HTTPException(403, "Нельзя удалить корень")
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.isfile(p) or os.path.islink(p):
        os.remove(p)
    else:
        raise HTTPException(404, "Не найдено")
    indexer.delete_path(storage.rel_of(p))
    _shares_hook(shares.delete_path, storage.rel_of(p))
    return {"ok": True}


# ---------- поиск (имена + содержимое через ripgrep) ----------

# ---------- поиск (PostgreSQL: содержимое + имя) ----------

@app.get("/api/search")
def api_search(q: str, path: str = "", _=Depends(require_auth)):
    q = q.strip()
    if not q:
        raise HTTPException(400, "Пустой запрос")
    if len(q) > 200:
        raise HTTPException(400, "Слишком длинный запрос")
    start = storage.safe(path)
    if not os.path.isdir(start):
        raise HTTPException(404, "Папка не найдена")
    if not indexer.ENABLED:
        raise HTTPException(500, "Индекс не настроен: в config.json нет раздела db")

    scope = storage.rel_of(start)
    if indexer.count() == 0:
        st = indexer.status()
        if not st["running"]:
            indexer.sync_start()
        return {"path": scope, "q": q, "items": [],
                "note": "Индекс пуст — запущено первичное индексирование, "
                        "повтори поиск через минуту"}
    items = indexer.search(q, scope_rel=scope)
    resp = {"path": scope, "q": q, "items": items}
    st = indexer.status()
    if st["running"]:
        resp["note"] = f"Индекс обновляется ({st['done']}/{st['total']}) — возможна неполнота"
    return resp


@app.get("/api/index")
def api_index(_=Depends(require_auth)):
    st = indexer.status()
    return {"enabled": indexer.ENABLED, "files": indexer.count(),
            "running": st["running"], "phase": st["phase"],
            "done": st["done"], "total": st["total"],
            "last_sync": st["last_sync"], "errors": st["errors"]}


@app.post("/api/index/sync")
def api_index_sync(_=Depends(require_auth)):
    started = indexer.sync_start()
    return {"ok": True, "started": started}


# ---------- общие ссылки: управление (владелец) ----------

class ShareBody(BaseModel):
    path: str


class RevokeBody(BaseModel):
    token: str


@app.get("/api/share")
def api_share_by_path(path: str = "", _=Depends(require_auth)):
    """Ссылки на конкретный объект (для диалога у строки)."""
    links = shares.list_by_rel(path)
    return {"links": [dict(l, url="/s/" + l["token"]) for l in links]}


@app.post("/api/share")
def api_share_create(body: ShareBody, _=Depends(require_auth)):
    p = storage.safe(body.path)
    if not os.path.exists(p):
        raise HTTPException(404, "Не найдено")
    rec = shares.create(storage.rel_of(p))
    return {"ok": True, **rec, "url": "/s/" + rec["token"]}


@app.post("/api/share/revoke")
def api_share_revoke(body: RevokeBody, _=Depends(require_auth)):
    removed = shares.remove(body.token)
    return {"ok": True, "removed": removed}


@app.get("/api/shares")
def api_shares_all(_=Depends(require_auth)):
    """Все активные ссылки (+ тип/размер/время объекта, если он жив)."""
    out = []
    for r in shares.all_():
        item = {"token": r["token"], "rel": r["rel"],
                "created": r["created"], "url": "/s/" + r["token"]}
        try:
            p = storage.safe(r["rel"])
            if os.path.isdir(p):
                st = os.stat(p)
                item.update(type="dir", name=os.path.basename(p.rstrip(os.sep)) or "Облако",
                            size=None, mtime=int(st.st_mtime))
            elif os.path.isfile(p):
                st = os.stat(p)
                item.update(type="file", name=os.path.basename(p),
                            size=st.st_size, mtime=int(st.st_mtime))
            else:
                item["type"] = "missing"
        except Exception:
            item["type"] = "missing"
        out.append(item)
    return {"shares": out}


# ---------- общие ссылки: публичный просмотр (без авторизации) ----------

def _guest(token: str, p: str):
    """(abs_share, abs_target, p_norm): корень шары и запрошенная цель внутри неё.
    404 — ссылки нет; 403 — попытка выйти за пределы шары."""
    rel = shares.rel_of(token)          # 404, если ссылка отозвана
    share = storage.safe(rel)           # внутри хранилища (реальный путь)
    if not os.path.lexists(share):
        raise HTTPException(404, "Ссылка недействительна или удалена")
    if not os.path.isdir(share):
        # расшарен файл — вложенных путей у него нет
        if (p or "").strip("/"):
            raise HTTPException(403, "У файла нет вложенных путей")
        return share, share, ""
    pn = (p or "").strip("/")
    if not pn:
        return share, share, ""
    target = os.path.realpath(os.path.join(share, *pn.split("/")))
    if target != share and not target.startswith(share.rstrip(os.sep) + os.sep):
        raise HTTPException(403, "Вне области общего доступа")
    return share, target, pn


@app.get("/s/{token}")
def s_share_page(token: str):
    """Ссылка: файл открывается сразу (inline), папка — страница просмотра."""
    share, _, _ = _guest(token, "")
    if os.path.isfile(share):
        media = mimetypes.guess_type(share)[0] or "application/octet-stream"
        return FileResponse(share, media_type=media,
                            filename=os.path.basename(share),
                            content_disposition_type="inline")
    return FileResponse(SHARE_INDEX, media_type="text/html")


@app.get("/api/s/{token}/list")
def s_share_list(token: str, p: str = ""):
    """Содержимое папки внутри шары. Пути элементов — относительно шары."""
    share, target, pn = _guest(token, p)
    if not os.path.isdir(target):
        raise HTTPException(404, "Папка не найдена")
    share_rel = shares.rel_of(token)
    if not share_rel:
        share_rel = ""  # расшарен корень
    out = []
    for it in storage.list_dir(target, hide_dot=HIDE_DOT):
        try:
            a = storage.safe(it["rel"])  # реальный путь (внутри хранилища)
        except HTTPException:
            continue
        if a != share and not a.startswith(share.rstrip(os.sep) + os.sep):
            continue  # symlink наружу области доступа — гостю не показываем
        nrel = it["rel"].replace("\\", "/")
        g = nrel[len(share_rel) + 1:] if share_rel else nrel
        out.append({"name": it["name"], "type": it["type"],
                    "size": it["size"], "mtime": it["mtime"], "rel": g})
    name = os.path.basename(share.rstrip(os.sep)) or "Облако"
    return {"name": name, "path": pn, "items": out}


@app.get("/api/s/{token}/file")
def s_share_file(token: str, p: str = ""):
    """Просмотр файла внутри шары (inline)."""
    _, target, _ = _guest(token, p)
    if not os.path.isfile(target):
        raise HTTPException(404, "Файл не найден")
    media = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media,
                        filename=os.path.basename(target),
                        content_disposition_type="inline")


@app.get("/api/s/{token}/download")
def s_share_download(token: str, p: str = ""):
    """Скачивание: файл как есть, папка — zip-архивом."""
    _, target, _ = _guest(token, p)
    if os.path.isfile(target):
        media = mimetypes.guess_type(target)[0] or "application/octet-stream"
        return FileResponse(target, media_type=media,
                            filename=os.path.basename(target),
                            content_disposition_type="attachment")
    if os.path.isdir(target):
        zpath, zname, cleanup = _zip_dir(target)
        return FileResponse(zpath, media_type="application/zip", filename=zname,
                            content_disposition_type="attachment",
                            background=BackgroundTask(cleanup))
    raise HTTPException(404, "Не найдено")
