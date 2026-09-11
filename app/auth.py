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
    expected пуст/None — логин не настроен, вход по одному паролю (старые конфиги)."""
    expected = (expected or "").strip()
    if not expected:
        return True
    return hmac.compare_digest((user or "").strip().casefold(), expected.casefold())


def make_token(secret: str, days: int) -> str:
    """Подписанный токен сессии: cloud.<expiry_unix>.hmac."""
    exp = int(time.time()) + days * 86400
    body = f"cloud.{exp}"
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def check_token(secret: str, token: str) -> bool:
    try:
        body, mac = token.rsplit(".", 1)
        if int(body.split(".")[1]) < time.time():
            return False
        expected = hmac.new(secret.encode(), body.encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, expected)
    except Exception:
        return False
