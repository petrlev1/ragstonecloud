"""Тесты удаления с прогрессом (app/deleter.py) — без веб-сервера.

Проверяем: подсчёт объёма, монотонность прогресса, отмену, «занято»,
удаление одиночного файла и то, что отменённое удаление не трогает остальное.

Запуск: ./venv/Scripts/python.exe test_delete.py     (Windows, локальная тест-копия)
        ./venv/bin/python test_delete.py            (Linux)
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import deleter  # noqa: E402

FAILED = []
BYTES_PER_FILE = 64 * 1024
FILES = 900


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ((" — " + str(extra)) if extra else ""))
    if not cond:
        FAILED.append(name)


def make_tree(root, files=FILES, subdirs=3, size=BYTES_PER_FILE):
    """Дерево: subdirs подпапок по (files/subdirs) файлов; + файл в корне и пустая папка."""
    blob = b"x" * size
    for d in range(subdirs):
        sub = os.path.join(root, "d%d" % d, "inner")
        os.makedirs(sub, exist_ok=True)
        for i in range(files // subdirs):
            with open(os.path.join(sub, "f%04d.bin" % i), "wb") as fh:
                fh.write(blob)
    with open(os.path.join(root, "root.bin"), "wb") as fh:
        fh.write(blob)
    os.makedirs(os.path.join(root, "empty"), exist_ok=True)


def tree_bytes(root):
    total = 0
    for r, _d, fs in os.walk(root):
        for n in fs:
            try:
                total += os.lstat(os.path.join(r, n)).st_size
            except OSError:
                pass
    return total


def wait_end(timeout=180):
    t0 = time.time()
    while deleter.status()["active"]:
        if time.time() - t0 > timeout:
            raise AssertionError("удаление не завершилось за %s с" % timeout)
        time.sleep(0.05)
    return deleter.status()["job"]


def test_full_delete():
    print("\n[1] полное удаление дерева: прогресс, объём, финал")
    base = tempfile.mkdtemp(prefix="deltest_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    make_tree(root)
    total = tree_bytes(root)
    expect_files = FILES + 1
    seen = []
    fin = []

    job = deleter.start("tree", root, on_finish=lambda j: fin.append("fin"))
    check("id операции выдан", job.id > 0, job.id)
    while deleter.status()["active"]:
        seen.append(deleter.status()["job"])
        time.sleep(0.05)
    end = wait_end()

    check("фаза завершения = done", end["phase"] == "done", end["phase"])
    check("всего байт посчитано", end["total_bytes"] == total, (end["total_bytes"], total))
    check("всего файлов посчитано", end["total_files"] == expect_files, end["total_files"])
    check("удалено файлов = всего", end["done_files"] == expect_files, end["done_files"])
    check("удалено байт = всего", end["done_bytes"] == total, end["done_bytes"])
    check("остаток нулевой", end["left_bytes"] == 0, end["left_bytes"])
    check("процент 100", end["percent"] == 100, end["percent"])
    check("ошибок нет", end["errors"] == 0, end["error_msgs"])
    check("папка удалена", not os.path.exists(root))
    check("on_finish вызван", fin.count("fin") == 1, fin.count("fin"))
    check("была фаза подсчёта", any(s["phase"] == "scan" for s in seen))
    check("была фаза удаления", any(s["phase"] == "delete" for s in seen))
    prog = [s for s in seen if s["phase"] == "delete"]
    check("прогресс не убывает",
          all(prog[i]["done_bytes"] <= prog[i + 1]["done_bytes"] for i in range(len(prog) - 1)))
    check("промежуточный прогресс виден (не один снимок)", len(prog) >= 2, len(prog))
    shutil.rmtree(base, ignore_errors=True)


def test_cancel():
    print("\n[2] отмена в середине удаления")
    base = tempfile.mkdtemp(prefix="deltest_cancel_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    make_tree(root, files=1200, subdirs=4)
    total = tree_bytes(root)

    deleter.start("tree", root, on_finish=lambda j: None)
    mid = None
    t0 = time.time()
    while time.time() - t0 < 30:
        s = deleter.status()["job"]
        if s["phase"] == "delete" and s["done_files"] > 0 and s["done_files"] < s["total_files"]:
            mid = s
            break
        time.sleep(0.02)
    check("дождались середины удаления", mid is not None,
          (mid or {}).get("done_files"))
    check("отмена принята", deleter.cancel() is True)
    end = wait_end()
    check("фаза = cancelled", end["phase"] == "cancelled", end["phase"])
    check("часть файлов уже удалена", end["done_files"] > 0, end["done_files"])
    check("остаток на диске есть", end["left_bytes"] > 0, end["left_bytes"])
    check("удалено + осталось = всего",
          end["done_bytes"] + end["left_bytes"] == end["total_bytes"],
          (end["done_bytes"], end["left_bytes"], end["total_bytes"]))
    check("дерево удалено не до конца", os.path.exists(root))
    check("что-то осталось на диске", tree_bytes(root) == end["left_bytes"],
          (tree_bytes(root), end["left_bytes"]))
    check("отменять нечего — повторная отмена False", deleter.cancel() is False)
    shutil.rmtree(base, ignore_errors=True)


def test_cancel_in_scan():
    print("\n[3] отмена во время подсчёта")
    base = tempfile.mkdtemp(prefix="deltest_scan_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    make_tree(root, files=2400, subdirs=6)
    deleter.start("tree", root, on_finish=lambda j: None)
    time.sleep(0.05)
    deleter.cancel()
    end = wait_end()
    check("фаза = cancelled", end["phase"] == "cancelled", end["phase"])
    check("ничего не удалено", end["done_files"] == 0, end["done_files"])
    check("дерево целое", os.path.isdir(root) and len(os.listdir(root)) == 8,
          len(os.listdir(root)))
    shutil.rmtree(base, ignore_errors=True)


def test_busy():
    print("\n[4] одновременные удаления запрещены")
    base = tempfile.mkdtemp(prefix="deltest_busy_")
    a, b = os.path.join(base, "a"), os.path.join(base, "b")
    os.makedirs(a)
    make_tree(a, files=1200, subdirs=4)
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


def test_single_file():
    print("\n[5] одиночный файл и симлинк на папку")
    base = tempfile.mkdtemp(prefix="deltest_one_")
    f = os.path.join(base, "one.bin")
    with open(f, "wb") as fh:
        fh.write(b"y" * 5000)
    job = deleter.start("one.bin", f)
    end = wait_end()
    check("файл удалён", not os.path.exists(f))
    check("фаза done", end["phase"] == "done", end["phase"])
    check("размер учтён", end["total_bytes"] == 5000 and end["done_bytes"] == 5000,
          (end["total_bytes"], end["done_bytes"]))
    check("один файл", end["total_files"] == 1 and end["done_files"] == 1)

    if hasattr(os, "symlink"):                      # на Windows нужен dev-mode — пропускаем молча
        target = os.path.join(base, "target")
        os.makedirs(target)
        with open(os.path.join(target, "in.bin"), "wb") as fh:
            fh.write(b"q" * 100)
        link = os.path.join(base, "link")
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            print("  SKIP симлинки: %s" % exc)
        else:
            deleter.start("link", link)
            end = wait_end()
            check("симлинк снят", not os.path.lexists(link))
            check("цель не тронута", os.path.isfile(os.path.join(target, "in.bin")))
            check("без ошибок", end["errors"] == 0, end["error_msgs"])

    out = sys.stdout
    print("\nstatus() без операции:", deleter.status()["active"], deleter.status()["job"] is not None)
    shutil.rmtree(base, ignore_errors=True)


def test_locked_file():
    print("\n[7] занятый файл: повторная попытка и честный счётчик ошибок")
    base = tempfile.mkdtemp(prefix="deltest_lock_")
    root = os.path.join(base, "tree")
    os.makedirs(root)
    files = []
    for i in range(5):
        p = os.path.join(root, "f%d.bin" % i)
        with open(p, "wb") as fh:
            fh.write(b"z" * 1000)
        files.append(p)

    # держим файл открытым: на Windows unlink падает (sharing violation),
    # отпускаем во время паузы перед повторной попыткой
    fh = open(files[0], "rb")
    deleter.start("tree", root, on_finish=lambda j: None)
    time.sleep(0.3)
    fh.close()
    end = wait_end()
    check("повторная попытка удалила занятый файл", end["errors"] == 0, end["error_msgs"])
    check("все файлы удалены", end["done_files"] == 5, end["done_files"])
    check("папка удалена", not os.path.exists(root))

    # а теперь занятый файл не отпускаем — ошибка должна остаться в отчёте
    root2 = os.path.join(base, "tree2")
    os.makedirs(root2)
    for i in range(3):
        with open(os.path.join(root2, "g%d.bin" % i), "wb") as fh2:
            fh2.write(b"z" * 1000)
    hold = open(os.path.join(root2, "g0.bin"), "rb")
    deleter.start("tree2", root2, on_finish=lambda j: None)
    end = wait_end()
    check("фаза done (не error) при частичном провале", end["phase"] == "done", end["phase"])
    check("ошибка посчитана", end["errors"] == 1, (end["errors"], end["error_msgs"]))
    check("остаток показан", end["left_files"] == 1, end["left_files"])
    check("папка осталась, часть файлов удалена",
          os.path.isdir(root2) and len(os.listdir(root2)) == 1, os.listdir(root2))
    hold.close()
    shutil.rmtree(base, ignore_errors=True)


def test_missing_path():
    print("\n[6] несуществующий путь")
    base = tempfile.mkdtemp(prefix="deltest_none_")
    missing = os.path.join(base, "nope")
    deleter.start("nope", missing)
    end = wait_end()
    check("фаза = error", end["phase"] == "error", end["phase"])
    check("есть текст ошибки", bool(end["error"]), end["error"])
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    t0 = time.time()
    test_full_delete()
    test_cancel()
    test_cancel_in_scan()
    test_busy()
    test_single_file()
    test_locked_file()
    test_missing_path()
    print("\n%s за %.1f с" % ("ВСЁ ОК" if not FAILED else "ПРОВАЛЫ: " + ", ".join(FAILED),
                              time.time() - t0))
    sys.exit(1 if FAILED else 0)
