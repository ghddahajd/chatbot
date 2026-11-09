"""Пароли сотрудников: scrypt из стандартной библиотеки, без обратимого хранения."""

import base64
import hashlib
import hmac
import re
import secrets

N, R, P = 2**15, 8, 3


def normalize_login(value: str) -> str:
    login = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@-]{2,63}", login):
        raise ValueError("Логин: 3–64 символа, латиница, цифры, точка, @, дефис или подчёркивание")
    return login


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("Пароль должен содержать от 12 до 128 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, maxmem=64 * 1024**2)
    return "scrypt-v1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 128:
        return False
    try:
        algorithm, salt, expected = encoded.split("$")
        salt = base64.b64decode(salt, validate=True)
        expected = base64.b64decode(expected, validate=True)
        if algorithm != "scrypt-v1" or len(salt) != 16 or len(expected) != 64:
            return False
        digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, maxmem=64 * 1024**2)
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False
