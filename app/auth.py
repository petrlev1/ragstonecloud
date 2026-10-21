"""Пароль (PBKDF2-SHA256, только стандартная библиотека) и подписанные сессии (HMAC)."""
import hashlib
import hmac
import secrets
import time

ITER_DEFAULT = 260_000


def make_hash(password: str, salt: str | None = None,
              iterations: int = ITER_DEFAULT) -> dict:
    """Возвращает {"salt", "hash", "iterations"} для хранения в config.json."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                             bytes.fromhex(salt), iterations)
    return {"salt": salt, "hash": dk.hex(), "iterations": iterations}


def check_password(password: str, rec: dict) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                             bytes.fromhex(rec["salt"]), rec["iterations"])
    return hmac.compare_digest(dk.hex(), rec["hash"])


def check_login(user: str, expected: str | None) -> bool:
    """Сверка логина без учёта регистра и пробелов по краям.
    expected пуст/None — логин не настроен, вход по одному паролю (старые конфиги).

    Сравниваем байты: hmac.compare_digest не принимает не-ASCII строки
    (кириллический логин раньше давал 500 вместо «неверный логин»).
    """
    expected = (expected or "").strip()
    if not expected:
        return True
    return hmac.compare_digest((user or "").strip().casefold().encode(),
                               expected.casefold().encode())


def make_token(secret: str, days: int, role: str = "owner", uid: int = 0) -> str:
    """Подписанный токен сессии: cloud.<expiry_unix>.<role>.<uid>.<hmac>.

    role: owner — владелец облака (весь сервер), user — пользователь RAG
    (uid = его id в rag_system.users, доступ только к своей папке).
    """
    exp = int(time.time()) + days * 86400
    body = f"cloud.{exp}.{role}.{int(uid)}"
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def parse_token(secret: str, token: str) -> dict | None:
    """Разбор сессии: {'role', 'uid'} либо None (битый/просроченный токен).

    Совместимость: старый формат cloud.<exp>.<hmac> (3 части) = владелец,
    поэтому ранее выданные сессии продолжают работать.
    """
    try:
        parts = (token or "").split(".")
        if len(parts) == 3:                      # cloud.<exp>.<mac> — старая сессия
            body, mac = f"{parts[0]}.{parts[1]}", parts[2]
            role, uid = "owner", 0
        elif len(parts) == 5:                    # cloud.<exp>.<role>.<uid>.<mac>
            body, mac = ".".join(parts[:4]), parts[4]
            role, uid = parts[2], int(parts[3])
        else:
            return None
        if int(body.split(".")[1]) < time.time():
            return None
        expected = hmac.new(secret.encode(), body.encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        if role not in ("owner", "user") or (role == "user" and uid <= 0):
            return None
        return {"role": role, "uid": uid}
    except Exception:
        return None


def check_token(secret: str, token: str) -> bool:
    return parse_token(secret, token) is not None
