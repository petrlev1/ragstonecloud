"""Фоновое удаление файлов и папок: прогресс (файлы/МБ), оценка остатка, отмена.

Удаление большого дерева (особенно по сети — через sshfs-маунт чужого сервера)
идёт минутами, поэтому /api/delete его НЕ ждёт: создаётся фоновая операция,
UI опрашивает /api/delete/status (сколько МБ всего, сколько уже удалено,
скорость, остаток) и может остановить её через /api/delete/cancel.
Одновременно допускается одна операция — так состояние на экране однозначно.

Порядок работы: сначала обход дерева (подсчёт файлов/байтов — «сколько всего
удалять»), затем обход снизу вверх с удалением, затем одна повторная попытка для
того, что не удалилось с первого раза (файл бывает занят антивирусом/индексатором,
на сетевом ФС — временный сбой). Отмена проверяется перед каждым файлом;
отменённое удаление оставляет недоудалённую часть дерева на диске как есть
(вернуть уже удалённое нельзя).
"""
import errno
import os
import threading
import time

KEEP_LAST = 300.0   # сколько секунд держим результат последнего удаления (для перезагрузки страницы)
MAX_ERRORS = 20     # сколько текстов ошибок храним (счётчик ошибок — полный)

_lock = threading.Lock()
_current = None     # активная операция (Job) или None
_last = None        # последняя завершённая (её показывает UI после F5)
_seq = 0


class Busy(RuntimeError):
    """Одновременные удаления не поддерживаются: уже идёт другое."""


class Job:
    """Одна операция удаления.

    Пишет состояние рабочий поток, читает HTTP-поток; в snapshot() уходит копия
    простых типов, отдельные счётчики (int) читаются атомарно.
    """

    def __init__(self, jid: int, rel: str, abs_path: str, on_finish=None):
        self.id = jid
        self.rel = rel                      # путь от корня хранилища, "/"-нотация
        self.abs = abs_path
        self.name = os.path.basename((rel or "").rstrip("/")) or (rel or "/")
        self.is_link = os.path.islink(abs_path)
        self.is_dir = (not self.is_link) and os.path.isdir(abs_path)
        self.size = 0
        if not self.is_dir:
            try:
                self.size = 0 if self.is_link else os.lstat(abs_path).st_size
            except OSError:
                self.size = 0

        self.phase = "scan"                 # scan | delete | done | cancelled | error
        self.error = None                   # текст фатальной ошибки
        self._failed: list[tuple[str, str, str, bool]] = []  # (путь, вид, сообщение, ignore_missing)
        self._walk_errors: list[str] = []                # обход каталога не удался
        self.total_files = 0                # сколько всего файлов (после подсчёта)
        self.total_dirs = 0
        self.total_bytes = 0                # сколько всего байт
        self.scanned_files = 0              # прогресс подсчёта
        self.scanned_bytes = 0
        self.done_files = 0                 # что уже удалено
        self.done_dirs = 0
        self.done_bytes = 0
        self.started = time.time()
        self.finished = None
        self._del0 = None                   # начало фазы удаления (для скорости/ETA)
        self.cancel_ev = threading.Event()
        self.on_finish = on_finish          # вызывается по завершении (индекс, ссылки)

    # ---------- ошибки и прогресс ----------

    @property
    def errors(self) -> int:
        return len(self._failed) + len(self._walk_errors)

    @property
    def error_msgs(self) -> list[str]:
        msgs = [f"{p}: {m}" for p, _kind, m, _ig in self._failed] + self._walk_errors
        return msgs[:MAX_ERRORS]

    def _on_walk_error(self, exc: OSError) -> None:
        self._walk_errors.append(f"{getattr(exc, 'filename', '') or '?'}: {exc.strerror or exc}")

    def _remove(self, path: str, kind: str, ignore_missing: bool = True) -> None:
        """Удалить один элемент ("file" | "dir"). Симлинк снимаем, цель не трогаем.

        ignore_missing: при обходе дерева «файла уже нет» — это успех (гонка,
        повторный обход); для одиночного объекта — честная ошибка.
        """
        if os.path.islink(path):
            kind = "link"
        try:
            if kind == "link":
                os.unlink(path)
            elif kind == "dir":
                os.rmdir(path)
            else:
                try:
                    self.done_bytes += os.lstat(path).st_size
                except OSError:
                    pass
                os.unlink(path)
        except FileNotFoundError:
            if not ignore_missing:
                self._failed.append((path, kind, "не найдено", ignore_missing))
                return
        except OSError as exc:
            # папку не убрать, пока внутри остались неудалённые файлы (ENOTEMPTY):
            # это следствие уже посчитанной ошибки, второй раз не считаем
            if kind == "dir" and exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return
            self._failed.append((path, kind, exc.strerror or str(exc), ignore_missing))
            return
        if kind == "dir":
            self.done_dirs += 1
        else:
            self.done_files += 1

    # ---------- снимок для UI ----------

    def snapshot(self) -> dict:
        now = time.time()
        end = self.finished or now
        active = self.phase in ("scan", "delete")
        dt = (end - self._del0) if self._del0 else 0.0
        speed = (self.done_bytes / dt) if (dt > 0.05 and self.done_bytes) else 0.0
        left_bytes = max(self.total_bytes - self.done_bytes, 0)
        eta = (left_bytes / speed) if (speed > 0 and active) else None
        step = self.total_bytes or self.total_files
        got = self.done_bytes if self.total_bytes else self.done_files
        percent = min(100, int(got * 100 / step)) if step else (100 if not active else 0)
        return {
            "id": self.id,
            "rel": self.rel,
            "name": self.name,
            "is_dir": self.is_dir,
            "phase": self.phase,
            "active": active,
            "cancelled": self.cancel_ev.is_set(),
            "total_files": self.total_files,
            "total_dirs": self.total_dirs,
            "total_bytes": self.total_bytes,
            "scanned_files": self.scanned_files,
            "scanned_bytes": self.scanned_bytes,
            "done_files": self.done_files,
            "done_dirs": self.done_dirs,
            "done_bytes": self.done_bytes,
            "left_bytes": left_bytes,
            "left_files": max(self.total_files - self.done_files, 0),
            "percent": percent,
            "speed": round(speed, 1),
            "eta": (round(eta, 1) if eta is not None else None),
            "elapsed": round(max(0.0, end - self.started), 1),
            "errors": self.errors,
            "error": self.error,
            "error_msgs": self.error_msgs,
            "started": self.started,
            "finished_at": self.finished,
        }


# ---------- API модуля ----------

def status() -> dict:
    """Текущая операция, либо результат последней (короткое время после конца)."""
    with _lock:
        job = _current
    if job is not None:
        return {"active": True, "job": job.snapshot()}
    with _lock:
        last = _last
    if last is not None and last.finished and (time.time() - last.finished) < KEEP_LAST:
        return {"active": False, "job": last.snapshot()}
    return {"active": False, "job": None}


def start(rel: str, abs_path: str, on_finish=None) -> Job:
    """Начать удаление в фоне. Busy — если уже идёт другое удаление."""
    global _current, _seq
    with _lock:
        if _current is not None:
            raise Busy("Уже идёт удаление «%s» — дождись его или отмени"
                       % (_current.name or "/"))
        _seq += 1
        job = Job(_seq, rel, abs_path, on_finish)
        _current = job
    threading.Thread(target=_run, args=(job,), daemon=True, name="delete-job").start()
    return job


def cancel() -> bool:
    """Попросить текущую операцию остановиться (перед следующим файлом)."""
    with _lock:
        job = _current
    if job is None:
        return False
    job.cancel_ev.set()
    return True


def busy() -> bool:
    with _lock:
        return _current is not None


# ---------- рабочий поток ----------

def _run(job: Job) -> None:
    global _current, _last
    try:
        if job.is_dir:
            _scan(job)
            if job.cancel_ev.is_set():
                job.phase = "cancelled"
            else:
                job.phase = "delete"
                job._del0 = time.time()
                _delete_tree(job)
                _retry_failed(job)
        else:                               # файл или симлинк — удаляем сразу
            job.total_files = 1
            job.total_bytes = job.size
            job.scanned_files = 1
            job.scanned_bytes = job.size
            job.phase = "delete"
            job._del0 = time.time()
            job._remove(job.abs, "file", ignore_missing=False)
            _retry_failed(job)
    except Exception as exc:                # поток не должен падать молча
        job.phase = "error"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        if job.phase in ("scan", "delete"):
            job.phase = "cancelled" if job.cancel_ev.is_set() else "done"
        if job.phase == "done" and job.errors and not (job.done_files or job.done_dirs):
            job.phase = "error"             # ничего удалить не удалось
            job.error = (job.error_msgs or ["Удаление не удалось"])[0]
        job.finished = time.time()
        with _lock:
            _current = None
            _last = job
        if job.on_finish:
            try:
                job.on_finish(job)
            except Exception as exc:        # хвосты (индекс/ссылки) не роняют результат
                job._walk_errors.append(f"on_finish: {exc}")


def _scan(job: Job) -> None:
    """Подсчёт: сколько всего предстоит удалить (файлы, папки, байты)."""
    for root, dirs, files in os.walk(job.abs, topdown=True, followlinks=False,
                                     onerror=job._on_walk_error):
        if job.cancel_ev.is_set():
            return
        job.total_dirs += len(dirs)
        for name in files:
            path = os.path.join(root, name)
            job.total_files += 1
            if not os.path.islink(path):
                try:
                    job.total_bytes += os.lstat(path).st_size
                except OSError:
                    pass
            # живые счётчики: в подсчёте видно, сколько уже найдено
            job.scanned_files = job.total_files
            job.scanned_bytes = job.total_bytes
        job.scanned_files = job.total_files
        job.scanned_bytes = job.total_bytes


def _delete_tree(job: Job) -> None:
    """Обход снизу вверх (как rmtree_safe: симлинк снимаем, цель не трогаем)."""
    for root, dirs, files in os.walk(job.abs, topdown=False, followlinks=False,
                                     onerror=job._on_walk_error):
        if job.cancel_ev.is_set():
            return
        for name in files:
            if job.cancel_ev.is_set():
                return
            job._remove(os.path.join(root, name), "file")
        for name in dirs:
            if job.cancel_ev.is_set():
                return
            job._remove(os.path.join(root, name), "dir")
    if job.cancel_ev.is_set():
        return
    job._remove(job.abs, "dir")


def _retry_failed(job: Job) -> None:
    """Одна повторная попытка для сбойных элементов.

    Файл бывает занят (антивирус, индексатор, чужой процесс) или на сетевом ФС
    вылетает временная ошибка — со второй попытки обычно удаляется. Что не
    удалось и со второй — остаётся в job._failed и попадает в счётчик ошибок.
    """
    if job.cancel_ev.is_set() or not job._failed:
        return
    fails = job._failed
    job._failed = []
    time.sleep(0.5)
    before = job.done_files + job.done_dirs
    for i, (path, kind, _msg, ig) in enumerate(fails):
        if job.cancel_ev.is_set():
            job._failed.extend(fails[i:])       # остаток даже не пробовали — он не удалён
            return
        job._remove(path, kind, ignore_missing=ig)
    # если повторная попытка что-то сняла, пустая папка-верхушка могла не убраться в
    # основном проходе (там её отказ от ENOTEMPTY ошибкой не считается) — пробуем снова
    if (job.done_files + job.done_dirs) > before and job.is_dir and os.path.isdir(job.abs):
        job._remove(job.abs, "dir")
