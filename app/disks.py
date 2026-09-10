"""«Диски» облака: список из config.json + место (занято/свободно) и тип носителя.

config.json:  "disks": [{"rel": "", "name": "Сервер Митино"}, ...]
  rel  — путь внутри корня хранилища ("" = сам корень);
  name — подпись в UI;
  type — необязательная подсказка для сетевых маунтов (SSD/HDD/…): для них
         локально устройство не определить; для локальных маунтов тип берётся
         из /sys/block/<dev>/queue/rotational автоматически.
"""
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FTimeout

ROOT = ""
DISKS: list[dict] = []
_dev_cache: dict[str, str] = {}              # abs_path -> тип носителя
_pool = ThreadPoolExecutor(max_workers=8)    # statvfs сетевого маунта может подвиснуть


def init(cfg: dict):
    global ROOT, DISKS
    ROOT = cfg["root"]
    DISKS = cfg.get("disks") or [{"rel": "", "name": "Облако"}]
    _dev_cache.clear()


def _abs(rel: str) -> str:
    return os.path.join(ROOT, rel) if rel else ROOT


def _usage(abs_path: str):
    """(total, used, free) в байтах; None — путь недоступен/подвис (сетевой маунт).

    POSIX — statvfs (used по bfree, free по bavail, как в df);
    Windows (тест-копия) — shutil.disk_usage (statvfs там нет).
    """
    def call():
        if hasattr(os, "statvfs"):
            st = os.statvfs(abs_path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - st.f_bfree * st.f_frsize
            return total, used, free
        total, used, free = shutil.disk_usage(abs_path)
        return total, used, free

    fut = _pool.submit(call)
    try:
        return fut.result(timeout=3)
    except (_FTimeout, OSError, ValueError):
        return None


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def _device_type(abs_path: str) -> str | None:
    """SSD / NVMe SSD / HDD для локального маунта; None — если это сеть (sshfs)."""
    src = _run("findmnt", "-no", "SOURCE", "--target", abs_path)
    if not src or not src.startswith("/dev/"):
        return None
    dev = src
    for _ in range(4):              # поднимаемся по LVM/разделам до физического диска
        parent = _run("lsblk", "-no", "PKNAME", dev).splitlines()
        if not parent or not parent[0].strip():
            break
        dev = "/dev/" + parent[0].strip()
    base = os.path.basename(dev)
    try:
        with open(f"/sys/block/{base}/queue/rotational") as f:
            rot = f.read().strip()
    except OSError:
        return None
    if rot == "0":
        return "NVMe SSD" if base.startswith("nvme") else "SSD"
    return "HDD"


def stats() -> list[dict]:
    out = []
    for d in DISKS:
        rel = d.get("rel", "")
        abs_path = _abs(rel)
        u = _usage(abs_path)
        total, used, free = u if u else (None, None, None)
        if abs_path not in _dev_cache:
            _dev_cache[abs_path] = _device_type(abs_path) or ""
        out.append({
            "rel": rel,
            "name": d.get("name") or (rel or "Облако"),
            "type": d.get("type") or _dev_cache[abs_path] or "сеть",
            "total": total, "used": used, "free": free,
        })
    return out
