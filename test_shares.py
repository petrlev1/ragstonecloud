# -*- coding: utf-8 -*-
"""Интеграционный тест расшаривания ragstonecloud (тест-ПК, 127.0.0.1:8080)."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8080"
failures = []


def out(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + ("  | " + extra if extra else ""))
    if not ok:
        failures.append(name)


cj = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener_guest = urllib.request.build_opener()  # без куки — чистый гость


def req(method, path, body=None, guest=False, data=None, ctype=None):
    url = BASE + path
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if data is not None:
        payload = data
    if ctype:
        headers["Content-Type"] = ctype
    r = urllib.request.Request(url, data=payload, headers=headers, method=method)
    op = opener_guest if guest else opener
    try:
        with op.open(r) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jget(path):
    st, b = req("GET", path)
    try:
        return st, json.loads(b.decode()) if b else None
    except Exception:
        return st, b.decode(errors="replace")


def jpost(path, body):
    st, b = req("POST", path, body=body)
    try:
        return st, json.loads(b.decode()) if b else None
    except Exception:
        return st, b.decode(errors="replace")


def gget(path):
    """Гость: GET без куки."""
    st, b = req("GET", path, guest=True)
    try:
        return st, json.loads(b.decode()) if b else None
    except Exception:
        return st, b.decode(errors="replace")


def gbody(path):
    """Гость: сырой GET без куки (для файлов/страниц)."""
    return req("GET", path, guest=True)


# --- вход владельца ---
st, b = req("POST", "/api/login", {"password": "test-pass"})
out("логин владельца", st == 200, str(st))

# --- шары ---
st, d_share = jpost("/api/share", {"path": "share_test"})
out("шара папки root", st == 200 and "token" in (d_share or {}), str(d_share))
D = (d_share or {}).get("token", "")

st, f_share = jpost("/api/share", {"path": "share_test/sub/doc.md"})
out("шара файла", st == 200 and "token" in (f_share or {}), str(f_share))
F = (f_share or {}).get("token", "")

st, s_share = jpost("/api/share", {"path": "share_test/sub"})
S = (s_share or {}).get("token", "")
out("шара вложенной папки", st == 200 and S, str(st))

# --- гость: страницы и листинги БЕЗ куки ---
st, b = gbody("/s/" + D)
out("страница папки гостю 200 html", st == 200 and b.startswith(b"<!doctype html>"), str(st))

st, body = gget("/api/s/" + D + "/list")
names = [i["name"] for i in (body or {}).get("items", [])] if body else []
out("list корня шары", st == 200 and "sub" in names and "readme.txt" in names, str(names))

st, body = gget("/api/s/" + D + "/list?p=" + urllib.parse.quote("sub"))
names = [i["name"] for i in (body or {}).get("items", [])] if body else []
rels = [i["rel"] for i in (body or {}).get("items", [])] if body else []
# rel считается от КОРНЯ шары (SPA строит p из него) — для p=sub это sub/deep, sub/doc.md
out("list вложенной папки, rel от корня шары",
    st == 200 and "sub/deep" in rels and "sub/doc.md" in rels, str(list(zip(names, rels))))

st, b = gbody("/api/s/" + D + "/file?p=" + urllib.parse.quote("sub/doc.md"))
out("файл inline гостю", st == 200 and b"hello doc" in b, str(st))

st, b = gbody("/s/" + F)
out("файловая шара открывается сразу", st == 200 and b"hello doc" in b, str(st))

st, body = gget("/api/s/" + S + "/list?p=" + urllib.parse.quote("deep"))
names = [i["name"] for i in (body or {}).get("items", [])] if body else []
out("шара подпапки видит только своё", st == 200 and "notes.txt" in names, str(names))

# --- защита: выход за пределы шары ---
for bad in ["..", "../..", "sub/../../.."]:
    st, body = gget("/api/s/" + D + "/list?p=" + urllib.parse.quote(bad, safe=""))
    err = body.get("error") if isinstance(body, dict) else body
    out("обход за пределы шары 403: p=" + bad, st == 403, str(st) + " " + str(err))

st, b = gbody("/api/s/" + D + "/download?p=" + urllib.parse.quote("sub/doc.md"))
out("скачивание файла гостю", st == 200, str(st))
st, b = gbody("/api/s/" + D + "/download?p=" + urllib.parse.quote("sub"))
out("zip папки гостю", st == 200 and b[:2] == b"PK", str(st) + " len=" + str(len(b)))

st, b = gbody("/s/no_such_token_zzz")
out("несуществующий токен 404", st == 404, str(st))

st, b = gbody("/api/me")
out("гость без куки не видит админку", st == 401, str(st))

# --- владелец: список/диалог ---
st, body = jget("/api/shares")
n = len((body or {}).get("shares", [])) if body else 0
out("api/shares: 3 ссылки", st == 200 and n == 3, str(n))

st, body = jget("/api/share?path=" + urllib.parse.quote("share_test/sub"))
n = len((body or {}).get("links", [])) if body else 0
out("ссылки конкретного объекта", st == 200 and n == 1, str(n))

# --- rename: ссылка едет за объектом ---
st, _ = jpost("/api/rename", {"path": "share_test/sub", "name": "sub2"})
out("rename ок", st == 200, str(st))
st, body = jget("/api/s/" + D + "/list?p=sub2")
out("шара следует за renamed-папкой", st == 200 and "doc.md" in [i["name"] for i in (body or {}).get("items", [])],
    str(st))
st, _ = jget("/api/s/" + D + "/list?p=sub")
out("старый путь после rename отдаёт 404", st == 404, str(st))

# --- move: переезд вложенной папки (дерево) ---
st, _ = jpost("/api/move", {"path": "share_test/sub2/deep", "dest": "share_test"})
out("move ок", st == 200, str(st))
# S = шара на sub2 (бывш. sub): deep уехал ИЗ неё в share_test — внутри остался doc.md
st, body = gget("/api/s/" + S + "/list")
names = [i["name"] for i in (body or {}).get("items", [])] if body else []
out("шара на sub2 после move: doc.md на месте, deep ушёл",
    st == 200 and "doc.md" in names and "deep" not in names, str(names))
st, body = gget("/api/s/" + D + "/list?p=deep")
out("root-шара видит перемещённую папку", st == 200 and "notes.txt" in [i["name"] for i in (body or {}).get("items", [])],
    str(st))

# --- revoke ---
st, _ = jpost("/api/share/revoke", {"token": F})
out("revoke файловой шары", st == 200, str(st))
st, b = req("GET", "/s/" + F)
out("после revoke ссылка 404", st == 404, str(st))

# --- delete папки: шара на неё и вложенное снимаются ---
st, _ = jpost("/api/delete", {"path": "share_test/sub2"})
out("delete папки ок", st == 200, str(st))
st, _ = jget("/s/" + S)
out("шара удалённой папки отдаёт 404", st == 404, str(st))
st, body = jget("/api/shares")
left = [s["rel"] for s in (body or {}).get("shares", [])] if body else []
out("осталась только root-шара", len(left) == 1 and left == ["share_test"], str(left))

# --- удаление фикстуры ---
st, _ = jpost("/api/delete", {"path": "share_test"})
out("фикстура удалена", st == 200, str(st))
st, _ = jget("/s/" + D)
out("root-шара после удаления 404", st == 404, str(st))

print("\nИТОГ:", "OK — все проверки прошли" if not failures else f"ПРОВАЛЕНО: {failures}")
sys.exit(1 if failures else 0)
