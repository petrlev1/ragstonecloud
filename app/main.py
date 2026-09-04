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

from app import auth, config as cfg_mod, indexer, storage

cfg = cfg_mod.load()
storage.ROOT = cfg["root"]
SECRET = cfg["secret"]
PW = cfg["password"]
SESSION_DAYS = int(cfg.get("session_days", 30))
HIDE_DOT = bool(cfg.get("hide_dot", True))
INDEX = os.path.join(cfg_mod.BASE, "static", "index.html")
LOGIN_COOKIE = "cl_session"

# полнотекстовый индекс (PostgreSQL): схема + фоновая синхронизация при старте
indexer.init(cfg)

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
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        raise HTTPException(429, "Слишком много попыток — подожди минуту")
    if not auth.check_password(body.password, PW):
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


@app.get("/api/download")
def api_download(path: str, _=Depends(require_auth)):
    p = storage.safe(path)
    if os.path.isfile(p):
        media = mimetypes.guess_type(p)[0] or "application/octet-stream"
        return FileResponse(p, media_type=media, filename=os.path.basename(p),
                            content_disposition_type="attachment")
    if os.path.isdir(p):
        tmp = tempfile.mkdtemp(prefix="cloud_zip_")
        base = os.path.basename(p.rstrip(os.sep)) or "cloud"
        zpath = shutil.make_archive(os.path.join(tmp, base), "zip",
                                    root_dir=os.path.dirname(p),
                                    base_dir=os.path.basename(p))

        def _cleanup():
            try:
                os.remove(zpath)
                os.rmdir(tmp)
            except OSError:
                pass

        return FileResponse(zpath, media_type="application/zip", filename=f"{base}.zip",
                            content_disposition_type="attachment",
                            background=BackgroundTask(_cleanup))
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
