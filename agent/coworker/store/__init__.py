"""Persistent state: SQLite tables, the audit chain and secrets.

Import from here for the common names:
    Store, Approval, StoreError       - the database facade and its types
    get_secret, set_secret            - the keyring-backed secret store

``legacy_import`` is deliberately not imported here. It depends on ``config``,
and ``config`` depends on this package for secrets, so importing it here would
make the two import each other.
"""
from .approval_rows import Approval
from .base import StoreError
from .db import Store
from .secrets import SecretStoreError, get_secret, set_secret

__all__ = [
    "Approval",
    "SecretStoreError",
    "Store",
    "StoreError",
    "get_secret",
    "set_secret",
]
