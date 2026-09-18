"""Symmetric encryption for the one field in this app that must stay
reversible: a monitored DNI/NIE/matrícula.

Everywhere else an identifier is touched (checks_free, rate limiting) only
a one-way hash is ever stored — see rate_limit.hash_identifier. This is the
sole exception, because the daily monitoring cron has to decrypt the value
to re-query the BOE with it. Key comes from TARGET_ENCRYPTION_KEY (a Fernet
key — generate one with `Fernet.generate_key()`), set as a Render env var
and a GitHub Actions secret, never committed to the repo.
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet

logger = logging.getLogger("vigila")

_key = os.environ.get("TARGET_ENCRYPTION_KEY")
if not _key:
    # Dev convenience only: an ephemeral key means anything encrypted this
    # run is unreadable after a restart. Production always sets the real
    # env var, so this path is never hit there.
    logger.warning("TARGET_ENCRYPTION_KEY not set — using an ephemeral key for this process only")
    _key = Fernet.generate_key().decode()

_fernet = Fernet(_key.encode())


def encrypt_value(value: str) -> str:
    return _fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_value(token: str) -> str:
    return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
