"""Регресс-тест области поиска (indexer.search): фильтр `path` должен действовать и на
совпадения по ИМЕНИ, и на совпадения по содержимому.

Баг (был): OR-группа условий в SQL не была обёрнута в скобки:
    WHERE (содержимое @ запрос) OR имя ILIKE …  AND (область)
из-за приоритета AND/OR область применялась только к ветке имени, и совпадения по
СОДЕРЖИМОМУ вытаскивали файлы из чужих папок (на проде: path=assistant&q=смета
отдавал файлы из data/…).

Тест не зависит от платформы: строки индекса создаются напрямую в PG с '/' в путях
(как на Linux-инстансе), после прогона удаляются.
Запуск: ./venv/Scripts/python.exe test_search_scope.py
"""
import json
import os
import sys

import psycopg

ROOT = os.path.dirname(os.path.abspath(__file__))
A, B = "scope_test_A", "scope_test_B"          # «папки», между которыми проверяем область
NAME_WORD = "сметарегресс"                     # уникальное слово, чтобы не мешать реальным данным
CONTENT_WORD = "содержимоерегресс"
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


def main():
    sys.path.insert(0, ROOT)
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    db = cfg["db"]
    conn = psycopg.connect(host=db["host"], port=db["port"], dbname=db["db"],
                           user=db["user"], password=db["password"])

    rows = [
        # имя: по одному совпадению в каждой «папке»
        (f"{A}/файл1-{NAME_WORD}.txt", f"файл1-{NAME_WORD}.txt", None),
        (f"{B}/файл2-{NAME_WORD}.txt", f"файл2-{NAME_WORD}.txt", None),
        # содержимое: тоже в обеих «папках»
        (f"{A}/текст1.txt", "текст1.txt", CONTENT_WORD),
        (f"{B}/текст2.txt", "текст2.txt", CONTENT_WORD),
    ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM files WHERE path LIKE %s OR path LIKE %s OR path IN (%s, %s)",
                    (A + "/%", B + "/%", A, B))
        # две «папки»
        for d in (A, B):
            cur.execute(
                """INSERT INTO files(path, name, mtime, size, content, tsv, is_dir)
                   VALUES (%s, %s, 0, NULL, NULL, to_tsvector('russian', ''), true)""",
                (d, d))
        for path, name, content in rows:
            cur.execute(
                """INSERT INTO files(path, name, mtime, size, content, tsv, is_dir)
                   VALUES (%s, %s, 0, 1, %s, to_tsvector('russian', %s), false)""",
                (path, name, content, content or ""))
    conn.commit()

    from app import indexer
    # подключаем поиск без init(): init запускает фоновую синхронизацию, а она вычистит
    # наши строки (их нет на диске) прямо во время прогона
    indexer.DB = db
    indexer.ROOT = cfg["root"]
    indexer.ENABLED = True

    print("\n== совпадения по ИМЕНИ ==")
    all_hits = [i["rel"] for i in indexer.search(NAME_WORD)]
    check("без области — видны обе папки",
          any(h.startswith(A + "/") for h in all_hits) and any(h.startswith(B + "/") for h in all_hits),
          all_hits)
    a_hits = [i["rel"] for i in indexer.search(NAME_WORD, scope_rel=A)]
    check("область A — только файлы A", a_hits and all(h.startswith(A + "/") for h in a_hits), a_hits)
    check("область A — файлов B нет", not any(h.startswith(B + "/") for h in a_hits), a_hits)
    b_hits = [i["rel"] for i in indexer.search(NAME_WORD, scope_rel=B)]
    check("область B — только файлы B", b_hits and all(h.startswith(B + "/") for h in b_hits), b_hits)

    print("\n== совпадения по СОДЕРЖИМОМУ ==")
    a_c = [i["rel"] for i in indexer.search(CONTENT_WORD, scope_rel=A)]
    check("область A — только A/текст1.txt", a_c == [f"{A}/текст1.txt"], a_c)
    a_both = [i["rel"] for i in indexer.search(CONTENT_WORD, scope_rel=A)]
    check("область A — файла B/текст2.txt нет", not any(h.startswith(B + "/") for h in a_both), a_both)

    print("\n== смешанный запрос (имя + содержимое в разных папках) ==")
    beide = [i["rel"] for i in indexer.search(NAME_WORD, scope_rel=A)]
    check("область A не отдаёт ничего из B", not any(h.startswith(B) for h in beide), beide)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM files WHERE path LIKE %s OR path LIKE %s OR path IN (%s, %s)",
                    (A + "/%", B + "/%", A, B))
    conn.commit()
    conn.close()

    print(f"\nИтого: {ok} ok, {fail} fail")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
