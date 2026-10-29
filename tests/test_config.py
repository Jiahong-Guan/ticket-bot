from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import TOKEN, VALID_CONFIG
from utils.config import Settings
from utils.exceptions import ConfigurationError


def load(path: Path, base: Path, **env: str) -> Settings:
    return Settings.load(path, {"DISCORD_TOKEN": TOKEN, **env}, base_dir=base)


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_valid_settings_load(settings: Settings) -> None:
    assert settings.bot_owner_ids == [42]
    assert settings.embed_theme.as_int("primary") == 0x5865F2
    assert settings.ticket_format.open.format(number=7) == "ticket-0007"
    assert settings.log_level == "INFO"


def test_relative_paths_resolve_against_base_dir(tmp_path: Path, config_file: Path) -> None:
    s = load(config_file, tmp_path, DB_PATH="./data/db")
    assert s.db_path == (tmp_path / "data" / "db").resolve()


def test_absolute_paths_are_kept(tmp_path: Path, config_file: Path) -> None:
    s = load(config_file, tmp_path, DB_PATH=str(tmp_path / "abs"))
    assert s.db_path == tmp_path / "abs"


def test_token_is_hidden_from_repr(settings: Settings) -> None:
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize("token", ["", "short", "your-bot-token"])
def test_bad_tokens_fail_fast(tmp_path: Path, config_file: Path, token: str) -> None:
    with pytest.raises(ConfigurationError):
        Settings.load(config_file, {"DISCORD_TOKEN": token}, base_dir=tmp_path)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load(tmp_path / "nope.json", tmp_path)


def test_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"bot_owner_ids": [000]}', encoding="utf-8")  # leading zeros are invalid JSON
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load(path, tmp_path)


def test_missing_key_is_reported(tmp_path: Path) -> None:
    data = {k: v for k, v in VALID_CONFIG.items() if k != "ticket_format"}
    with pytest.raises(ConfigurationError, match="ticket_format"):
        load(write(tmp_path, data), tmp_path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load(write(tmp_path, {**VALID_CONFIG, "typo_key": 1}), tmp_path)


@pytest.mark.parametrize("colour", ["ZZZZZZ", "FFF", "1234567"])
def test_bad_hex_colour(tmp_path: Path, colour: str) -> None:
    data = {**VALID_CONFIG, "embed_theme": {**VALID_CONFIG["embed_theme"], "primary": colour}}
    with pytest.raises(ConfigurationError):
        load(write(tmp_path, data), tmp_path)


def test_hash_prefix_is_accepted(tmp_path: Path) -> None:
    data = {**VALID_CONFIG, "embed_theme": {**VALID_CONFIG["embed_theme"], "primary": "#abcdef"}}
    assert load(write(tmp_path, data), tmp_path).embed_theme.primary == "ABCDEF"


@pytest.mark.parametrize("template", ["ticket-{nope}", "ticket-{}", "{number:bad}"])
def test_bad_ticket_template(tmp_path: Path, template: str) -> None:
    data = {**VALID_CONFIG, "ticket_format": {"open": template, "closed": "c-{number}"}}
    with pytest.raises(ConfigurationError):
        load(write(tmp_path, data), tmp_path)


@pytest.mark.parametrize("limit", [0, -1, 100])
def test_ticket_limit_bounds(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ConfigurationError):
        load(write(tmp_path, {**VALID_CONFIG, "max_open_tickets_per_user": limit}), tmp_path)


def test_bad_log_level(tmp_path: Path, config_file: Path) -> None:
    with pytest.raises(ConfigurationError, match="log level"):
        load(config_file, tmp_path, LOG_LEVEL="LOUD")


def test_log_level_is_normalised(tmp_path: Path, config_file: Path) -> None:
    assert load(config_file, tmp_path, LOG_LEVEL="debug").log_level == "DEBUG"
