"""Тесты правки txt в облаке: /api/save_text, кодировка/перевод строки, права, гостевые ссылки.

Нужен запущенный сервер (стенд или локальная копия) и папка с тестовыми файлами:
  заметка.txt (utf-8, LF), windows.txt (cp1251, CRLF), большой.txt (3 МБ),
  заметка.md, картинка.png — см. make_test_media.py / .stand-setup.py.
Авторизация — кука из config.json рядом со скриптом (пароль не трогаем).

Запуск:
  CLOUD_TEST_BASE=http://127.0.0.1:8090 ./venv/bin/python test_edit.py
  ./venv/Scripts/python.exe test_edit.py            (локальная копия на 8080)
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("CLOUD_TEST_BASE", "http://127.0.0.1:8080")
ROOT = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.environ.get("CLOUD_TEST_DIR", "тест-правка")
DATA_ROOT = os.environ.get("CLOUD_TEST_ROOT", "")     # где лежат файлы (для сверки с диском)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


def cfg():
    return json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))


def opener(role="owner", uid=0, cookie=True):
    jar = http.cookiejar.CookieJar()
    if cookie:
        sys.path.insert(0, ROOT)
        from app import auth
        token = auth.make_token(cfg()["secret"], 30, role, uid)
        jar.set_cookie(http.cookiejar.Cookie(0, "cl_session", token, None, False,
                                             "127.0.0.1", False, False, "/", True,
                                             False, None, False, None, None, {}))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(op, url, data=None, method=None):
    """(статус, json-тело)."""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(BASE + url, data=body, method=method or ("POST" if body else "GET"))
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with op.open(req, timeout=30) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:
        return status, {"raw": raw[:200]}


def disk(rel):
    root = DATA_ROOT or cfg()["root"]
    return os.path.join(root, *rel.replace("\\", "/").split("/"))


def listing(op):
    st, d = call(op, "/api/list?path=" + urllib.parse.quote(FOLDER))
    assert st == 200, (st, d)
    return {it["name"]: it["rel"] for it in d["items"]}


def main():
    owner = opener()
    guest = opener(cookie=False)
    files = listing(owner)
    check("листинг тестовой папки", "заметка.txt" in files, str(sorted(files)))

    note = files["заметка.txt"]
    win = files["windows.txt"]
    big = files["большой.txt"]
    md = files["заметка.md"]
    png = files["картинка.png"]

    # --- чтение ---
    st, d = call(owner, "/api/text?path=" + urllib.parse.quote(note))
    check("предпросмотр txt читается", st == 200 and "первая строка" in d.get("text", ""), str(d)[:120])

    # --- сохранение: utf-8, LF ---
    new_text = "первая строка (правка)\nвторая строка\n"
    st, d = call(owner, "/api/save_text", {"path": note, "text": new_text})
    check("сохранение txt: 200", st == 200 and d.get("ok"), str(d)[:150])
    raw = open(disk(note), "rb").read()
    check("файл на диске обновлён", raw == new_text.encode("utf-8"), repr(raw[:60]))
    check("utf-8 + LF сохранены", d.get("encoding") == "utf-8" and d.get("eol") == "LF", str(d)[:120])
    check("размер в ответе верный", d.get("size") == len(raw), f"{d.get('size')} vs {len(raw)}")

    # --- кодировка cp1251 и CRLF ---
    st, d = call(owner, "/api/text?path=" + urllib.parse.quote(win))
    check("cp1251/CRLF читается", st == 200 and d.get("encoding") == "cp1251" and "\r\n" in d.get("text", ""),
          str(d)[:120])
    st, d = call(owner, "/api/save_text", {"path": win, "text": "строка один\nстрока два (правка)\n"})
    check("сохранение cp1251: 200", st == 200 and d.get("encoding") == "cp1251", str(d)[:150])
    raw = open(disk(win), "rb").read()
    check("кодировка cp1251 сохранена", raw == "строка один\r\nстрока два (правка)\r\n".encode("cp1251"), repr(raw[:80]))
    check("перевод строки CRLF сохранён", d.get("eol") == "CRLF", str(d)[:120])

    # --- символ вне cp1251 → файл становится utf-8 ---
    st, d = call(owner, "/api/save_text", {"path": win, "text": "строка\n✓ галочка\n"})
    raw = open(disk(win), "rb").read()
    check("символ вне cp1251 → utf-8", st == 200 and d.get("encoding") == "utf-8" and b"\xe2\x9c\x93" in raw,
          str(d)[:120])

    # --- отказы ---
    st, d = call(owner, "/api/save_text", {"path": md, "text": "# новое\n"})
    check("не .txt → 415", st == 415, f"{st} {d}")
    st, d = call(owner, "/api/save_text", {"path": png, "text": "шум"})
    check("картинка → 415", st == 415, f"{st} {d}")
    st, d = call(owner, "/api/save_text", {"path": big, "text": "мало"})
    check("файл > 2 МБ → 413", st == 413, f"{st} {d}")
    st, d = call(owner, "/api/save_text", {"path": "../../etc/passwd", "text": "x"})
    check("выход за хранилище → 403", st == 403, f"{st} {d}")
    st, d = call(owner, "/api/save_text", {"path": FOLDER + "/нет-такого.txt", "text": "x"})
    check("несуществующий файл → 404", st == 404, f"{st} {d}")

    # --- без сессии (гость) ---
    st, d = call(guest, "/api/save_text", {"path": note, "text": "взлом\n"})
    check("без входа сохранение → 401", st == 401, f"{st} {d}")

    # --- гостевая ссылка: читать можно, писать некуда ---
    st, d = call(owner, "/api/share", {"path": note})
    token = d.get("token")
    check("ссылка на txt создана", st == 200 and token, f"{st} {d}")
    st, d = call(guest, "/api/s/" + token + "/text")
    check("гость читает текст по ссылке", st == 200 and "правка" in d.get("text", ""), str(d)[:120])
    st, d = call(guest, "/api/s/" + token + "/save_text", {"path": "", "text": "взлом\n"})
    check("гостевой ручки записи нет (404/405)", st in (404, 405), f"{st} {d}")
    st, d = call(guest, "/api/share", {"path": note})
    check("гость не может создать ссылку", st == 401, f"{st} {d}")
    st, _ = call(owner, "/api/share/revoke", {"token": token})
    check("ссылка закрыта", st == 200, str(st))

    # --- индекс поиска видит новый текст ---
    st, d = call(owner, "/api/search?q=" + urllib.parse.quote("правка"))
    rels = [it.get("rel", "").replace("\\", "/") for it in d.get("items", [])]
    check("поиск находит новый текст", st == 200 and note.replace("\\", "/") in rels, str(rels)[:200])

    # --- пользователь RAG: только своя папка, квота считается ---
    before = open(disk(note), "rb").read()
    st, d = call(opener("user", 7), "/api/save_text", {"path": FOLDER + "/заметка.txt", "text": "чужое\n"})
    check("чужой файл пользователю RAG недоступен (403/404)", st in (403, 404), f"{st} {d}")
    check("чужой файл не изменён", open(disk(note), "rb").read() == before)
    st, d = call(opener("user", 7), "/api/list?path=")
    kb_files = [it["rel"] for it in d.get("items", []) if it.get("type") == "file" and it["name"].endswith(".txt")]
    check("своя папка видна пользователю RAG", st == 200 and kb_files, f"{st} {d}"[:150])
    if kb_files:
        st, d = call(opener("user", 7), "/api/save_text", {"path": kb_files[0], "text": "текст базы (правка)\n"})
        check("свой файл kb пользователю RAG сохраняется", st == 200, f"{st} {d} {kb_files[0]}")

    print(f"\n{ok} OK, {fail} FAIL")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
