"""Shared fixtures: an isolated COWORKER_HOME, a fake keyring and a store per test.

Every test runs with its own home folder, so nothing reads or writes the user's
real config, store or keyring. The keyring is replaced by a dict, so no test
writes a real credential. Tests that need a machine without a keyring replace
the backend themselves.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from coworker.store import Store
from coworker.store import secrets as secrets_module


class FakeKeyring:
    """Dict-backed stand-in for the password functions of the keyring package."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.entries:
            raise KeyError(username)  # the real package raises when an entry is absent
        del self.entries[(service, username)]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "coworker-home"
    home.mkdir()
    monkeypatch.setenv("COWORKER_HOME", str(home))
    for name in list(os.environ):
        if name.startswith("COWORKER_") and name != "COWORKER_HOME":
            monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(secrets_module, "_backend", lambda: fake)
    return fake


@pytest.fixture
def store_path(isolated_home: Path) -> Path:
    return isolated_home / "store.db"


@pytest.fixture
def store(store_path: Path) -> Iterator[Store]:
    db = Store(store_path)
    yield db
    db.close()
