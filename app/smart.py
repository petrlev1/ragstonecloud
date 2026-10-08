"""Проверка здоровья дисков сервера (SMART) — через внешний root-хелпер.

Приложению нужен root, чтобы читать SMART, поэтому оно НЕ вызывает smartctl само:
в sudoers разрешён ровно один путь (NOPASSWD) — helper `cloud-disks`. Подсунуть
произвольную команду через веб нельзя: helper сам проверяет аргументы и работает
только с физическими дисками этой машины.

Команда вызова берётся из config.json ("smart_helper"), по умолчанию:
    sudo -n /usr/local/sbin/cloud-disks
Если helper не установлен (например, тест-копия на Windows), /api/smart вернёт
понятную ошибку — остальное облако продолжает работать.
"""
import json
import os
import shlex
import subprocess

HELPER = "sudo -n /usr/local/sbin/cloud-disks"
TIMEOUT = 120
CLOUD = []          # диски облака: [{"name", "rel", "abs"}]


def init(cfg: dict):
    """Запомнить команду helper'а и диски облака (для привязки диск → облачная папка)."""
    global HELPER, CLOUD
    HELPER = (cfg.get("smart_helper") or HELPER).strip()
    CLOUD = []
    root = cfg["root"]
    for d in cfg.get("disks") or []:
        rel = d.get("rel", "")
        CLOUD.append({
            "name": d.get("name") or (rel or "Облако"),
            "rel": rel,
            "abs": os.path.join(root, rel) if rel else root,
        })


def _call(args=()) -> tuple[dict | None, str | None]:
    """Вызвать helper. (данные, ошибка) — ровно одно из двух не None."""
    try:
        proc = subprocess.run(shlex.split(HELPER) + list(args),
                              capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return None, ("Проверка дисков не настроена на этой машине "
                      "(helper cloud-disks не установлен)")
    except subprocess.TimeoutExpired:
        return None, "Проверка дисков не ответила вовремя"
    except OSError as exc:
        return None, f"Не удалось запустить проверку: {exc}"

    out = (proc.stdout or "").strip()
    if not out:
        # stdout пуст — helper даже не запустился (нет файла, нет правила sudo NOPASSWD).
        # Свой JSON-ответ об ошибке helper печатает в stdout, поэтому сюда не попадает.
        tail = [s.strip() for s in (proc.stderr or "").strip().splitlines() if s.strip()]
        proc_name = shlex.split(HELPER)[0] if HELPER else "helper"
        return None, (f"Проверка дисков недоступна: {proc_name} не запустился"
                      + (f" ({tail[-1]})" if tail else ""))
    try:
        data = json.loads(out)
    except ValueError:
        return None, "Проверка дисков вернула неожиданный ответ"
    if not isinstance(data, dict):
        return None, "Проверка дисков вернула неожиданный ответ"
    return data, None


def _local_mount(path: str) -> str | None:
    """Точка монтирования пути, если он лежит на ЛОКАЛЬНОМ носителе.

    Сетевые маунты (sshfs к другому серверу, nfs, cifs) не принадлежат ни одному
    физическому диску этой машины — для них вернём None, чтобы не приписать чужое.
    """
    try:
        proc = subprocess.run(["findmnt", "-no", "SOURCE,FSTYPE,TARGET", "-T", path],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None                       # не Linux (например, тест-копия на Windows)
    parts = (proc.stdout or "").strip().split()
    if len(parts) < 3:
        return None
    source, fstype, target = parts[0], parts[1], parts[-1]
    if fstype in ("fuse.sshfs", "nfs", "nfs4", "cifs", "smb3", "fuse.rclone"):
        return None
    if not (source.startswith("/dev/") or source.startswith("UUID=")):
        return None
    return target


def _assign_cloud(disks: list[dict]) -> None:
    """Разложить диски облака по физическим: облачный диск показываем у того диска,
    на котором лежит его точка монтирования. Сетевые — ни у кого."""
    for d in disks:
        d["cloud"] = []
    by_mount: dict[str, list[str]] = {}
    for c in CLOUD:
        mount = _local_mount(c["abs"])
        if mount:
            by_mount.setdefault(os.path.realpath(mount), []).append(c["name"])
    for d in disks:
        for m in d.get("mounts") or []:
            for name in by_mount.get(os.path.realpath(m), []):
                if name not in d["cloud"]:
                    d["cloud"].append(name)


def check() -> dict:
    """Сводка SMART: {ok, host, time, disks: [...]} либо {ok: false, error}."""
    data, err = _call()
    if err:
        return {"ok": False, "error": err, "disks": []}
    _assign_cloud(data.get("disks") or [])
    if data.get("ok") is not False:
        data["ok"] = True
    return data


def start_test(dev: str) -> dict:
    """Запустить короткий self-test диска (прогресс видно в /api/smart)."""
    dev = (dev or "").strip()
    if not dev.startswith("/dev/") or len(dev) > 60 or " " in dev:
        return {"ok": False, "error": "Некорректное устройство"}
    data, err = _call(["--test", dev])
    if err:
        return {"ok": False, "error": err}
    return data
