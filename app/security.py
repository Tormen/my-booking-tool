"""Tokens, password/PIN hashing, CSRF, and a tiny in-memory rate limiter.
Stdlib only: secrets + hashlib.scrypt (available since Python 3.6).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque


# ---- random tokens (cancel links, sessions) --------------------------------

def new_token() -> str:
    """256 bits of randomness, URL-safe. Never store this raw -- hash it."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


# ---- GDPR erasure: keyed email hash ----------------------------------------
# A bare sha256(email) is NOT safe pseudonymization -- email addresses are
# low-entropy/guessable, so an attacker can just hash a dictionary of likely
# addresses and match. Keying the hash with a secret pepper (kept only in
# secrets/erasure_pepper, never copied into the archive) makes it
# unreversible without that pepper, which is what makes "archived with a
# hashed email" a real erasure rather than security theatre.

def hash_email_for_erasure(email: str, pepper: bytes) -> str:
    digest = hmac.new(pepper, email.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
    return f"erased:{digest}"


def is_erased_email(value: str) -> bool:
    return value.startswith("erased:")


# ---- PIN / password hashing (scrypt) ---------------------------------------
# scrypt params kept modest: this protects a 6-digit PIN (small keyspace,
# already rate-limited below) and an admin password. n=2**14 is fast enough
# for interactive login on a small VPS while still being real work per guess.

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_secret(secret: str, salt: bytes | None = None) -> tuple[str, str]:
    """Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        secret.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return digest.hex(), salt.hex()


def verify_secret(secret: str, hash_hex: str, salt_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.scrypt(
        secret.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return hmac.compare_digest(digest.hex(), hash_hex)


def hash_admin_password(password: str) -> str:
    """Single-string form ('salt$hash') for storing in admin_password_hash file."""
    h, s = hash_secret(password)
    return f"{s}${h}"


def verify_admin_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.strip().split("$", 1)
    except ValueError:
        return False
    return verify_secret(password, hash_hex, salt_hex)


# ---- CSV formula-injection guard -------------------------------------------

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_field(value: str) -> str:
    """Prefix a leading ' if the value would be interpreted as a formula
    by Excel/LibreOffice when the CSV is opened there later."""
    if value and value[0] in _DANGEROUS_PREFIXES:
        return "'" + value
    return value


# ---- rate limiting ----------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter, in-memory. Fine for a single small process;
    resets on restart, which is an acceptable trade-off for this app's size."""

    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        q = self._hits[key]
        while q and now - q[0] > self.window_seconds:
            q.popleft()
        if len(q) >= self.max_attempts:
            return False
        q.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def retry_after(self, key: str, now: float | None = None) -> float:
        """Seconds until `key` would be allowed again (0 if it's allowed
        right now) -- purely informational, doesn't consume an attempt the
        way allow() does. For a login form's visible lockout countdown
        (see app/webapp.py's _lockout_countdown_script): call this right
        after allow() returns False, passing the same `now`, so the two
        agree on what "now" was."""
        now = time.time() if now is None else now
        q = self._hits[key]
        while q and now - q[0] > self.window_seconds:
            q.popleft()
        if len(q) < self.max_attempts:
            return 0.0
        return max(0.0, self.window_seconds - (now - q[0]))
