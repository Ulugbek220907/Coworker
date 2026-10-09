"""Secrets live in the OS keyring, never in a file Coworker writes.

Entries use the keyring service ``Coworker`` and the secret's name as the user
name. When the keyring is missing or refuses, reading falls back to the
environment variable ``COWORKER_<NAME>``, so a machine without a keyring can
still run with secrets supplied by its service manager. Writing without a
keyring is an error rather than a quiet fallback to a plain file: a plain-text
copy of a key is the failure this module exists to prevent.

Two read functions differ in one case. ``get_secret`` is for values that can be
replaced or supplied elsewhere, so a keyring that fails to answer falls back to
the environment. ``read_secret`` is for values whose absence means something:
a keyring that fails to answer must raise, because treating the failure as "no
entry" makes the caller create a new one over the real one.

No function here logs a secret value. Errors name only the secret.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("secrets")

SERVICE = "Coworker"


class SecretStoreError(RuntimeError):
    """A secret could not be read or written through the keyring. Nothing was stored anywhere else."""


def _backend() -> Any | None:
    """The keyring module, or None when it is not installed."""
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def _env_name(name: str) -> str:
    return f"COWORKER_{name.upper()}"


def _keyring_read(backend: Any, name: str) -> str | None:
    """The keyring entry, or None when there is none. Raises SecretStoreError when the keyring fails."""
    try:
        return backend.get_password(SERVICE, name) or None
    except Exception as exc:  # keyring backends raise their own error types
        raise SecretStoreError(f"the keyring could not be read for {name}") from exc


def read_secret(name: str) -> str | None:
    """The keyring value, else the environment variable, else None.

    A keyring that is installed but fails to answer raises SecretStoreError, and
    the environment is not consulted. Only an answer of "no entry" falls through.
    """
    backend = _backend()
    if backend is not None:
        value = _keyring_read(backend, name)
        if value:
            return value
    return os.environ.get(_env_name(name)) or None


def get_secret(name: str) -> str | None:
    """The keyring value for ``name``, else the environment variable, else None."""
    try:
        return read_secret(name)
    except SecretStoreError:
        log.warning("the keyring could not be read for %s; trying the environment", name)
        return os.environ.get(_env_name(name)) or None


def keyring_has_entry(name: str) -> bool:
    """True when the keyring itself holds ``name``. The environment is not consulted.

    Raises SecretStoreError when the keyring fails to answer. With no keyring
    installed there is no entry, so the answer is False.
    """
    backend = _backend()
    return backend is not None and _keyring_read(backend, name) is not None


def set_secret(name: str, value: str) -> None:
    """Store ``value`` in the keyring. An empty value removes the entry."""
    backend = _backend()
    if backend is None:
        raise SecretStoreError(f"no keyring is installed, so {name} cannot be stored")
    if not value:
        try:
            backend.delete_password(SERVICE, name)
        except Exception:  # an entry that is already absent is the state we want
            pass
        return
    try:
        backend.set_password(SERVICE, name, value)
    except Exception as exc:
        raise SecretStoreError(f"the keyring refused to store {name}") from exc
