"""Scrypt password encoding, verification, and upgrade policy."""

from __future__ import annotations

import hashlib
import hmac
import secrets

SCRYPT_N = 2**15

SCRYPT_R = 8

SCRYPT_P = 3

SCRYPT_MAXMEM = 128 * 1024 * 1024


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _verify_password(encoded: str, password: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _password_needs_rehash(encoded: str) -> bool:
    parts = encoded.split("$", 5)
    return (
        len(parts) != 6
        or parts[0] != "scrypt"
        or parts[1:4] != [str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)]
    )
