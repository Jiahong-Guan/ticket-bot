from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from utils.config import Settings
from utils.database import Database

VALID_CONFIG = {
    "dev_guild_id": 123456789,
    "bot_owner_ids": [42],
    "activity": {
        "type": "playing",
        "name": "Ticket Bot"
    },
    "embed_theme": {
        "primary": "5865F2",
        "success": "57F287",
        "error": "ED4245",
        "warning": "FEE75C",
    },
    "ticket_format": {"open": "ticket-{number:04d}", "closed": "closed-{number:04d}"},
    "max_open_tickets_per_user": 1,
    "support_can_manage_messages": True,
}
TOKEN = "x" * 40


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(VALID_CONFIG),
        encoding="utf-8"
    )
    return path


@pytest.fixture
def settings(tmp_path: Path, config_file: Path) -> Settings:
    return Settings.load(
        config_file,
        {"DISCORD_TOKEN": TOKEN},
        base_dir=tmp_path
    )


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "db")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def bot(db: Database, settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(
        db=db,
        settings=settings
    )
