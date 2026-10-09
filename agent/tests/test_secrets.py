"""Secrets: keyring first, environment second, never a file, never a log line."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from coworker.store import secrets as secrets_module
from coworker.store.secrets import (
    SecretStoreError, get_secret, keyring_has_entry, read_secret, set_secret,
)


class _Refusing:
    def get_password(self, service: str, username: str) -> str:
        raise RuntimeError("the keyring is locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("access denied")

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("access denied")


def test_round_trip_goes_through_the_keyring(fake_keyring) -> None:
    set_secret("llm_api_key", "sk-test-value-123")
    assert get_secret("llm_api_key") == "sk-test-value-123"
    assert fake_keyring.entries[("Coworker", "llm_api_key")] == "sk-test-value-123"


def test_missing_secret_is_none() -> None:
    assert get_secret("nothing_here") is None


def test_environment_is_used_when_the_keyring_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COWORKER_LLM_API_KEY", "env-value")
    assert get_secret("llm_api_key") == "env-value"


def test_keyring_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COWORKER_RELAY_TOKEN", "from-env")
    set_secret("relay_token", "from-keyring")
    assert get_secret("relay_token") == "from-keyring"


def test_without_a_keyring_reads_work_and_writes_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_module, "_backend", lambda: None)
    monkeypatch.setenv("COWORKER_AUDIT_KEY", "env-audit")
    assert get_secret("audit_key") == "env-audit"
    assert get_secret("other") is None
    with pytest.raises(SecretStoreError):
        set_secret("llm_api_key", "x")


def test_keyring_read_error_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(secrets_module, "_backend", lambda: _Refusing())
    monkeypatch.setenv("COWORKER_LLM_API_KEY", "env-value")
    with caplog.at_level(logging.WARNING, logger="secrets"):
        assert get_secret("llm_api_key") == "env-value"
    assert "env-value" not in caplog.text


def test_keyring_write_error_is_raised_as_a_secret_store_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_module, "_backend", lambda: _Refusing())
    with pytest.raises(SecretStoreError):
        set_secret("llm_api_key", "value")


def test_empty_value_removes_the_entry(fake_keyring) -> None:
    set_secret("llm_api_key", "v")
    set_secret("llm_api_key", "")
    assert get_secret("llm_api_key") is None
    set_secret("llm_api_key", "")  # removing an entry that is already absent is fine


def test_values_never_reach_a_file(isolated_home: Path) -> None:
    set_secret("llm_api_key", "sk-VERYSECRET-99")
    for path in isolated_home.rglob("*"):
        if path.is_file():
            assert b"sk-VERYSECRET-99" not in path.read_bytes()


def test_values_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    set_secret("llm_api_key", "sk-LOGGED-VALUE")
    get_secret("llm_api_key")
    assert "sk-LOGGED-VALUE" not in caplog.text


def test_read_secret_returns_the_keyring_value_then_none(fake_keyring) -> None:
    set_secret("audit_key", "k-1")
    assert read_secret("audit_key") == "k-1"
    assert read_secret("nothing_here") is None


def test_read_secret_raises_instead_of_falling_back_when_the_keyring_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed read is not an absent entry: the caller must be able to refuse, not guess."""
    monkeypatch.setattr(secrets_module, "_backend", lambda: _Refusing())
    monkeypatch.setenv("COWORKER_AUDIT_KEY", "env-audit")
    with pytest.raises(SecretStoreError):
        read_secret("audit_key")


def test_keyring_has_entry_ignores_the_environment(fake_keyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COWORKER_AUDIT_KEY", "env-audit")
    assert keyring_has_entry("audit_key") is False
    set_secret("audit_key", "k-1")
    assert keyring_has_entry("audit_key") is True


def test_keyring_has_entry_raises_when_the_keyring_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_module, "_backend", lambda: _Refusing())
    with pytest.raises(SecretStoreError):
        keyring_has_entry("audit_key")
