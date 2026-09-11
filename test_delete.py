"""Тесты удаления с прогрессом (app/deleter.py) — без веб-сервера.

Проверяем: подсчёт объёма, монотонность прогресса, отмену (в подсчёте и в
удалении), запрет параллельных операций, одиночный файл, симлинки, занятый
файл (повторная попытка), несуществующий путь.

Отмена проверяется «на лету»: тест ждёт подходящий снимок и сразу отменяет.
На очень быстром диске удаление может закончиться раньше — тогда
соответствующие проверки помечаются SKIP (размер дерева задаётся DELTEST_COUNT,
по умолчанию 60 000 файлов).

Запуск: ./venv/Scripts/python.exe test_delete.py     (Windows)
        ./venv/bin/python test_delete.py            (Linux)
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import deleter  # noqa: E402

COUNT_BIG = int(os.environ.get("DELTEST_COUNT", "60000"))   # для тестов отмены
FAILED = []
SKIPPED = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ((" — " + str(extra)) if extra else ""))
    if not cond:
        FAILED.append(name)


def skip(name, why):
    print("  SKIP " + name + " — " + why)
    SKIPPED.append(name)


def snap():
    return deleter.status()["job"]


def wait_end(timeout=600):
    t0 = time.time()
    while deleter.status()["active"]:
        if time.time() - t0 > timeout:
            raise AssertionError("удаление не завершилось за %s с" % timeout)
        time.sleep(0.01)
    return snap()


def make_tree(root, count, subdirs=8, size=64, keep_open=None):
    """Дерево: subdirs подпапок по count/subdirs файлов + файл в корне и пустая папка."""
    blob = b"x" * size
    per = max(1, count // subdirs)
    made = 0
    for d in range(subdirs):
        sub = os.path.join(root, "d%d" % d, "inner")
        os.makedirs(sub, exist_ok=True)
        for i in range(per):
            with open(os.path.join(sub, "f%05d.bin" % i), "wb") as fh:
                fh.write(blob)
            made += 1
    p = os.path.join(root, "root.bin")
    with open(p, "wb") as fh:
        fh.write(blob)
    made += 1
    os.makedirs(os.path.join(root, "empty"), exist_ok=True)
    return made, subdirs + 1


def tree_bytes(root):
    total = 0
    for r, _d, fs in os.walk(root):
        for n in fs:
            try:
                total += os.lstat(os.path.join(r, n)).st_size
            except OSError:
                pass
    return total


def cancel_when(pred, timeout=120, poll=0.01):
    """Ждёт снимок, удовлетворяющий pred, и отменяет.

    Возвращает (сработало, финальный снимок). Если операция успела завершиться
    до того, как pred выполнился — (False, финальный снимок).
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = snap()
        if s is None or not s["active"]:
            return False, s
        if pred(s):
            deleter.cancel()
            return True, wait_end()
        time.sleep(poll)
    raise AssertionError("не дождались состояния для отмены")


def test_full_delete():
    print("\n[1] полное удаление дерева: подсчёт, прогресс, финал")
    base = tempfile.mkdtemp(prefix="deltest_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    made, dirs = make_tree(root, 20000)
    total = tree_bytes(root)
    seen, fin = [], []

    job = deleter.start("tree", root, on_finish=lambda j: fin.append("fin"))
    check("id операции выдан", job.id > 0, job.id)
    while deleter.status()["active"]:
        seen.append(snap())
        time.sleep(0.01)
    end = wait_end()

    check("фаза завершения = done", end["phase"] == "done", end["phase"])
    check("всего байт посчитано", end["total_bytes"] == total, (end["total_bytes"], total))
    check("всего файлов посчитано", end["total_files"] == made, end["total_files"])
    check("удалено файлов = всего", end["done_files"] == made, end["done_files"])
    check("удалено байт = всего", end["done_bytes"] == total, end["done_bytes"])
    check("остаток нулевой", end["left_bytes"] == 0, end["left_bytes"])
    check("процент 100", end["percent"] == 100, end["percent"])
    check("ошибок нет", end["errors"] == 0, end["error_msgs"])
    check("папка удалена", not os.path.exists(root))
    check("on_finish вызван", fin.count("fin") == 1, fin.count("fin"))
    check("была фаза подсчёта", any(s["phase"] == "scan" for s in seen))
    prog = [s for s in seen if s["phase"] == "delete"]
    if not prog:
        skip("была фаза удаления", "дерево удалилось между опросами (быстрый диск)")
    else:
        check("была фаза удаления", True)
    check("прогресс не убывает",
          all(prog[i]["done_bytes"] <= prog[i + 1]["done_bytes"] for i in range(len(prog) - 1)))
    if len(prog) < 2:
        skip("промежуточный прогресс виден", "меньше двух срезов фазы удаления")
    else:
        check("промежуточный прогресс виден (%d срезов)" % len(prog), True)
    shutil.rmtree(base, ignore_errors=True)


def test_cancel_mid_delete():
    print("\n[2] отмена в середине удаления (%d файлов)" % COUNT_BIG)
    base = tempfile.mkdtemp(prefix="deltest_cancel_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    made, _dirs = make_tree(root, COUNT_BIG, subdirs=12, size=1024)
    total = tree_bytes(root)

    deleter.start("tree", root, on_finish=lambda j: None)
    hit, end = cancel_when(lambda s: s["phase"] == "delete" and 0 < s["done_files"] < s["total_files"])
    if not hit:
        skip("отмена в середине удаления", "дерево удалилось целиком раньше отмены (%s из %s файлов)"
             % (end["done_files"], end["total_files"]))
    else:
        check("фаза = cancelled", end["phase"] == "cancelled", end["phase"])
        check("часть файлов уже удалена", end["done_files"] > 0, end["done_files"])
        check("остаток на диске есть", end["left_bytes"] > 0, end["left_bytes"])
        check("удалено + осталось = всего",
              end["done_bytes"] + end["left_bytes"] == end["total_bytes"],
              (end["done_bytes"], end["left_bytes"], end["total_bytes"]))
        check("дерево удалено не до конца", os.path.exists(root))
        if os.path.exists(root):
            check("остаток на диске совпадает с отчётом", tree_bytes(root) == end["left_bytes"],
                  (tree_bytes(root), end["left_bytes"]))
        check("отменять нечего — повторная отмена False", deleter.cancel() is False)
    shutil.rmtree(base, ignore_errors=True)


def test_cancel_in_scan():
    print("\n[3] отмена во время подсчёта (%d файлов)" % COUNT_BIG)
    base = tempfile.mkdtemp(prefix="deltest_scan_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    make_tree(root, COUNT_BIG, subdirs=12, size=1024)
    before = sorted(os.listdir(root))

    deleter.start("tree", root, on_finish=lambda j: None)
    hit, end = cancel_when(lambda s: s["phase"] == "scan")
    if not hit:
        skip("отмена во время подсчёта", "подсчёт прошёл быстрее опроса")
    else:
        check("фаза = cancelled", end["phase"] == "cancelled", end["phase"])
        check("ничего не удалено", end["done_files"] == 0, end["done_files"])
        check("дерево целое", os.path.isdir(root) and sorted(os.listdir(root)) == before,
              os.listdir(root) if os.path.isdir(root) else "нет папки")
    shutil.rmtree(base, ignore_errors=True)


def test_busy():
    print("\n[4] одновременные удаления запрещены")
    base = tempfile.mkdtemp(prefix="deltest_busy_")
    a, b = os.path.join(base, "a"), os.path.join(base, "b")
    os.makedirs(a)
    make_tree(a, COUNT_BIG, subdirs=12, size=1024)
    with open(b, "wb") as fh:
        fh.write(b"z" * 10)
    deleter.start("a", a, on_finish=lambda j: None)
    try:
        deleter.start("b", b, on_finish=lambda j: None)
        check("второе удаление отклонено", False)
    except deleter.Busy as exc:
        check("второе удаление отклонено", True)
        check("в тексте есть имя текущего объекта", "a" in str(exc), str(exc))
    deleter.cancel()
    wait_end()
    shutil.rmtree(base, ignore_errors=True)


def test_single_file_and_links():
    print("\n[5] одиночный файл и симлинки")
    base = tempfile.mkdtemp(prefix="deltest_one_")
    f = os.path.join(base, "one.bin")
    with open(f, "wb") as fh:
        fh.write(b"y" * 5000)
    end = (deleter.start("one.bin", f), wait_end())[1]
    check("файл удалён", not os.path.exists(f))
    check("фаза done", end["phase"] == "done", end["phase"])
    check("размер учтён", end["total_bytes"] == 5000 and end["done_bytes"] == 5000,
          (end["total_bytes"], end["done_bytes"]))
    check("один файл", end["total_files"] == 1 and end["done_files"] == 1)

    # дерево с симлинками: на файл, на папку, наружу и битый
    tree = os.path.join(base, "tree")
    outside = os.path.join(base, "outside")
    os.makedirs(os.path.join(tree, "sub"))
    os.makedirs(outside)
    with open(os.path.join(tree, "sub", "in.bin"), "wb") as fh:
        fh.write(b"q" * 100)
    with open(os.path.join(outside, "keep.bin"), "wb") as fh:
        fh.write(b"q" * 100)
    try:
        os.symlink(os.path.join(tree, "sub", "in.bin"), os.path.join(tree, "link_file"))
        os.symlink(os.path.join(tree, "sub"), os.path.join(tree, "link_dir"))
        os.symlink(os.path.join(outside, "keep.bin"), os.path.join(tree, "link_out"))
        os.symlink(os.path.join(tree, "nowhere"), os.path.join(tree, "link_broken"))
    except (OSError, NotImplementedError) as exc:
        skip("симлинки в дереве", "не создать ссылку: %s" % exc)
    else:
        end = (deleter.start("tree", tree), wait_end())[1]
        check("дерево с симлинками удалено", not os.path.exists(tree))
        check("фаза done, ошибок нет", end["phase"] == "done" and end["errors"] == 0,
              (end["phase"], end["error_msgs"]))
        check("цель ссылки наружу не тронута",
              os.path.isfile(os.path.join(outside, "keep.bin")))
        check("ассерт: содержимое цели цело",
              open(os.path.join(outside, "keep.bin"), "rb").read() == b"q" * 100)
    shutil.rmtree(base, ignore_errors=True)


def test_locked_file():
    print("\n[6] занятый файл: повторная попытка и честный счётчик ошибок")
    base = tempfile.mkdtemp(prefix="deltest_lock_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    files = []
    for i in range(5):
        p = os.path.join(root, "f%d.bin" % i)
        with open(p, "wb") as fh:
            fh.write(b"z" * 1000)
        files.append(p)

    fh = open(files[0], "rb")          # держим открытым
    deleter.start("tree", root, on_finish=lambda j: None)
    time.sleep(0.3 if os.name == "nt" else 0.05)
    fh.close()                          # отпускаем до повторной попытки
    end = wait_end()
    if os.name == "nt":
        check("повторная попытка удалила занятый файл", end["errors"] == 0, end["error_msgs"])
        check("все файлы удалены", end["done_files"] == 5, end["done_files"])
        check("папка удалена", not os.path.exists(root))
    else:
        check("открытый файл не мешает удалению (POSIX)", end["errors"] == 0, end["error_msgs"])
        check("папка удалена", not os.path.exists(root))

    if os.name == "nt":
        # занятый файл не отпускаем: ошибка должна остаться в отчёте, папка — остаться
        root2 = os.path.join(base, "tree2")
        os.makedirs(root2)
        for i in range(3):
            with open(os.path.join(root2, "g%d.bin" % i), "wb") as fh2:
                fh2.write(b"z" * 1000)
        hold = open(os.path.join(root2, "g0.bin"), "rb")
        end = (deleter.start("tree2", root2), wait_end())[1]
        check("фаза done (не error) при частичном провале", end["phase"] == "done", end["phase"])
        check("ошибка посчитана ровно одна", end["errors"] == 1,
              (end["errors"], end["error_msgs"]))
        check("остаток показан", end["left_files"] == 1, end["left_files"])
        check("папка осталась, часть файлов удалена",
              os.path.isdir(root2) and os.listdir(root2) == ["g0.bin"], os.listdir(root2))
        hold.close()
    shutil.rmtree(base, ignore_errors=True)


def test_missing_path():
    print("\n[7] несуществующий путь")
    base = tempfile.mkdtemp(prefix="deltest_none_")
    end = (deleter.start("nope", os.path.join(base, "nope")), wait_end())[1]
    check("фаза = error", end["phase"] == "error", end["phase"])
    check("есть текст ошибки", bool(end["error"]), end["error"])
    shutil.rmtree(base, ignore_errors=True)


def test_last_status():
    print("\n[8] результат последней операции доступен (для F5)")
    st = deleter.status()
    check("active=false", st["active"] is False)
    check("снимок последней операции есть", st["job"] is not None and st["job"]["id"] > 0,
          (st["job"] or {}).get("id"))
    check("в снимке есть rel/phase/percent",
          all(k in (st["job"] or {}) for k in ("rel", "phase", "percent", "left_bytes")))


if __name__ == "__main__":
    t0 = time.time()
    test_full_delete()
    test_cancel_mid_delete()
    test_cancel_in_scan()
    test_busy()
    test_single_file_and_links()
    test_locked_file()
    test_missing_path()
    test_last_status()
    print("\n%s за %.1f с%s" % (
        "ВСЁ ОК" if not FAILED else "ПРОВАЛЫ: " + ", ".join(FAILED),
        time.time() - t0,
        ("  (SKIP: %d)" % len(SKIPPED)) if SKIPPED else ""))
    sys.exit(1 if FAILED else 0)
