"""Тесты галереи/просмотрщика (миниатюры, тексты, гостевые ручки).

Нужен запущенный локальный сервер: C:\\cloud\\cloud, 127.0.0.1:8080
и тестовые файлы C:\\cloud\\data\\тест-галерея (make_test_media.py).
Авторизация — кука из config.json (пароль не трогаем).
Запуск: ./venv/Scripts/python.exe test_gallery.py
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8080"
ROOT = os.path.dirname(os.path.abspath(__file__))
FOLDER = "тест-галерея"          # rel-пути элементов берём из листинга (на Windows — с обратным слэшем)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


def opener(cookie=True):
    jar = http.cookiejar.CookieJar()
    if cookie:
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
        sys.path.insert(0, ROOT)
        from app import auth
        token = auth.make_token(cfg["secret"], 30)
        jar.set_cookie(http.cookiejar.Cookie(0, "cl_session", token, None, False,
                                             "127.0.0.1", False, False, "/", True,
                                             False, None, False, None, None, {}))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def req(op, path, data=None, method=None):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method or ("POST" if data else "GET"))
    if body:
        r.add_header("Content-Type", "application/json")
    try:
        with op.open(r, timeout=60) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def hget(h, name):
    """Заголовки приходят в нижнем регистре (uvicorn) — сравнение без учёта регистра."""
    for k, v in h.items():
        if k.lower() == name.lower():
            return v
    return ""


def main():
    owner = opener(True)
    guest = opener(False)

    print("\n== вход и листинг ==")
    st, b, _ = req(guest, "/api/list")
    check("/api/list без куки -> 401", st == 401, st)
    st, b, _ = req(owner, "/api/list?path=" + urllib.parse.quote(FOLDER))
    items = json.loads(b)["items"] if st == 200 else []
    check("листинг тест-папки", st == 200 and len(items) >= 12, f"{st} {len(items)}")
    check("в листинге есть миниатюризуемые картинки",
          any(i["name"].startswith("фото-") for i in items))

    print("\n== миниатюры (/api/thumb) ==")
    jpg_rel = next(i["rel"] for i in items if i["name"] == "фото-1.jpg")
    png_rel = next(i["rel"] for i in items if i["name"] == "прозрачный.png")
    gif_rel = next(i["rel"] for i in items if i["name"] == "анимация.gif")
    txt_rel = next(i["rel"] for i in items if i["name"] == "заметки.txt")
    dir_rel = FOLDER
    t1 = "/api/thumb?path=" + urllib.parse.quote(jpg_rel) + "&w=240"
    st, b, h = req(owner, t1)
    check("jpg -> 200 image/jpeg", st == 200 and hget(h, "content-type") == "image/jpeg", f"{st} {hget(h,'content-type')}")
    check("отдаётся с кэш-заголовком", "max-age" in hget(h, "cache-control"), hget(h, "cache-control"))
    check("jpeg-магия (FF D8)", b[:2] == b"\xff\xd8", b[:4])
    st2, b2, _ = req(owner, t1)
    check("повтор отдаётся из кэша (те же байты)", b2 == b)
    st, bs, _ = req(owner, "/api/thumb?path=" + urllib.parse.quote(jpg_rel) + "&w=64")
    check("w=64 меньше файл, чем w=240", st == 200 and len(bs) < len(b), f"{len(bs)} {len(b)}")
    st, b, _ = req(owner, "/api/thumb?path=" + urllib.parse.quote(png_rel))
    check("png -> 200 (альфа на белом)", st == 200 and b[:2] == b"\xff\xd8", st)
    st, b, _ = req(owner, "/api/thumb?path=" + urllib.parse.quote(gif_rel))
    check("gif -> 200", st == 200, st)
    st, b, _ = req(owner, "/api/thumb?path=" + urllib.parse.quote(txt_rel))
    check("не-картинка -> 415", st == 415, st)
    st, b, _ = req(owner, "/api/thumb?path=" + urllib.parse.quote(dir_rel))
    check("папка -> 404", st == 404, st)
    st, b, _ = req(owner, "/api/thumb?path=" + urllib.parse.quote(FOLDER + "/нет.png"))
    check("несуществующий -> 404", st == 404, st)
    st, b, _ = req(owner, "/api/thumb?path=" + urllib.parse.quote("../secret.jpg"))
    check("выход за root -> 403", st == 403, st)

    print("\n== тексты (/api/text) ==")
    st, b, _ = req(owner, "/api/text?path=" + urllib.parse.quote(txt_rel))
    d = json.loads(b) if st == 200 else {}
    check("utf-8 текст прочитан", st == 200 and "utf-8" in d.get("text", ""), st)
    check("кодировка utf-8", d.get("encoding") == "utf-8", d.get("encoding"))
    st, b, _ = req(owner, "/api/text?path=" + urllib.parse.quote(FOLDER + "/старое-cp1251.txt"))
    d = json.loads(b) if st == 200 else {}
    check("cp1251 распознан", d.get("encoding") == "cp1251", d.get("encoding"))
    check("cp1251 текст читается по-русски", "ёжик" in d.get("text", ""), d.get("text", "")[:40])
    st, b, _ = req(owner, "/api/text?path=" + urllib.parse.quote(dir_rel))
    check("текст папки -> 404", st == 404, st)

    print("\n== гостевая шара ==")
    st, b, _ = req(owner, "/api/share", {"path": FOLDER}, "POST")
    share = json.loads(b) if st == 200 else {}
    token = share.get("token", "")
    check("ссылка создана", st == 200 and token, f"{st} {share}")
    if token:
        st, b, _ = req(guest, f"/api/s/{token}/list")
        g_items = json.loads(b)["items"] if st == 200 else []
        check("гостю виден листинг", st == 200 and len(g_items) >= 12, f"{st} {len(g_items)}")
        grel = next((i["rel"] for i in g_items if i["name"] == "фото-1.jpg"), "")
        gtxt = next((i["rel"] for i in g_items if i["name"] == "заметки.txt"), "")
        st, b, h = req(guest, f"/api/s/{token}/thumb?p=" + urllib.parse.quote(grel))
        check("гостевая миниатюра 200 jpeg", st == 200 and b[:2] == b"\xff\xd8", st)
        st, b, _ = req(guest, f"/api/s/{token}/text?p=" + urllib.parse.quote(gtxt))
        d = json.loads(b) if st == 200 else {}
        check("гостевой текст 200", st == 200 and "utf-8" in d.get("text", ""), st)
        st, b, _ = req(guest, f"/api/s/{token}/thumb?p=../../etc/passwd")
        check("гость вне шары -> 403", st == 403, st)
        st, b, _ = req(guest, f"/api/s/{token}/thumb?p=" + urllib.parse.quote("нет.jpg"))
        check("гость: файла нет -> 404", st == 404, st)
        st, b, _ = req(owner, "/api/share/revoke", {"token": token}, "POST")
        check("ссылка отозвана", st == 200 and json.loads(b).get("removed") == 1, st)
        st, b, _ = req(guest, f"/api/s/{token}/thumb?p=" + urllib.parse.quote(grel))
        check("после отзыва -> 404", st == 404, st)

    print("\n== кэш миниатюр ==")
    cache = os.path.join(ROOT, ".thumbcache")
    n = len(os.listdir(cache)) if os.path.isdir(cache) else 0
    check(".thumbcache наполнен", n >= 4, n)

    print(f"\nИтого: {ok} ok, {fail} fail")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
