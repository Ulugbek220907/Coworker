"""Config: COWORKER_HOME, secrets kept out of the file, the new keys and the surface the flat modules use."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from coworker.config import CAP_FIND, Config, config_dir
from coworker.store import secrets as secrets_module


def test_config_dir_honours_coworker_home(isolated_home: Path) -> None:
    assert config_dir() == isolated_home
    assert isolated_home.is_dir()


def test_save_never_writes_the_secrets(isolated_home: Path, fake_keyring) -> None:
    path = isolated_home / "config.json"
    cfg = Config(path)
    cfg.set("llm_api_key", "sk-SECRET-KEY-1")
    cfg.set("relay_token", "relay-SECRET-2")
    cfg.set("llm_model", "deepseek-chat")  # an ordinary save
    text = path.read_text(encoding="utf-8")
    assert "SECRET-KEY-1" not in text and "relay-SECRET-2" not in text
    stored = json.loads(text)
    assert "llm_api_key" not in stored and "relay_token" not in stored
    assert cfg.get("llm_api_key") == "sk-SECRET-KEY-1"
    assert fake_keyring.entries[("Coworker", "llm_api_key")] == "sk-SECRET-KEY-1"


def test_a_new_instance_reads_the_secret_back_from_the_keyring(isolated_home: Path) -> None:
    path = isolated_home / "config.json"
    Config(path).set("llm_api_key", "k-persisted")
    assert Config(path).get("llm_api_key") == "k-persisted"


def test_save_drops_secrets_even_when_they_are_in_memory(isolated_home: Path) -> None:
    path = isolated_home / "config.json"
    cfg = Config(path)
    cfg.data["llm_api_key"] = "in-memory-only"
    cfg.save()
    assert "in-memory-only" not in path.read_text(encoding="utf-8")


def test_plaintext_secrets_from_an_old_file_move_to_the_keyring(isolated_home: Path, fake_keyring) -> None:
    path = isolated_home / "config.json"
    path.write_text(json.dumps({"llm_api_key": "old-key", "relay_token": "old-relay", "name": "PC1"}),
                    encoding="utf-8")
    cfg = Config(path)
    assert fake_keyring.entries[("Coworker", "llm_api_key")] == "old-key"
    assert fake_keyring.entries[("Coworker", "relay_token")] == "old-relay"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "llm_api_key" not in stored and "relay_token" not in stored
    assert stored["name"] == "PC1"
    assert cfg.get("relay_token") == "old-relay"


def test_without_a_keyring_a_secret_lasts_for_this_run_only(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(secrets_module, "_backend", lambda: None)
    path = isolated_home / "config.json"
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = Config(path)
        cfg.set("llm_api_key", "run-only-key")
    assert cfg.get("llm_api_key") == "run-only-key"
    assert "run-only-key" not in path.read_text(encoding="utf-8")
    assert "run-only-key" not in caplog.text
    assert Config(path).get("llm_api_key") == ""


def test_new_keys_have_their_defaults(isolated_home: Path) -> None:
    cfg = Config(isolated_home / "config.json")
    assert cfg.get("autonomy") == "ask_for_writes"
    assert cfg.get("disabled_families") == []
    assert cfg.get("recycle_verified") is False
    assert cfg.get("scratch_dir") == ""


def test_scratch_dir_is_under_coworker_home_and_created(isolated_home: Path) -> None:
    cfg = Config(isolated_home / "config.json")
    assert cfg.scratch_dir == isolated_home / "scratch"
    assert cfg.scratch_dir.is_dir()


def test_scratch_dir_can_be_overridden(isolated_home: Path, tmp_path: Path) -> None:
    cfg = Config(isolated_home / "config.json")
    other = tmp_path / "elsewhere"
    cfg.set("scratch_dir", str(other))
    assert cfg.scratch_dir == other and other.is_dir()


def test_existing_surface_used_by_the_flat_modules_still_works(isolated_home: Path) -> None:
    cfg = Config(isolated_home / "config.json")
    assert cfg.authorize(42) is True
    assert cfg.authorize(42) is False
    assert cfg.chats == [42]
    assert cfg.caps(42) == [CAP_FIND]
    cfg.set_caps(42, ["office", CAP_FIND])
    assert cfg.allows(42, "office") is True
    assert cfg.allows(42, "browser") is False
    cfg.revoke(42)
    assert cfg.chats == []
    assert cfg.is_blocked("C:/Users/me/passwords.txt") is True
    assert cfg.max_file_bytes == 45 * 1024 * 1024
    assert cfg.ws_url == "ws://127.0.0.1:8000/ws/agent"
    assert cfg.configured is False
