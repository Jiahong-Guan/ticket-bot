from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from utils.exceptions import ConfigurationError

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_PLACEHOLDER_TOKEN = "your-bot-token"


class EmbedTheme(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: str
    success: str
    error: str
    warning: str

    @field_validator("primary", "success", "error", "warning")
    @classmethod
    def _valid_hex(cls, value: str) -> str:
        cleaned = value.lstrip("#")

        try:
            int(cleaned, 16)
        except ValueError as exc:
            raise ValueError(f"'{value}' is not a valid hex colour") from exc

        if len(cleaned) != 6:
            raise ValueError(f"'{value}' must be exactly 6 hex digits")
        return cleaned.upper()

    def as_int(self, name: str) -> int:
        return int(getattr(self, name), 16)


class TicketFormat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    open: str
    closed: str

    @field_validator("open", "closed")
    @classmethod
    def _valid_template(cls, value: str) -> str:
        try:
            rendered = value.format(number=1)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError("template may only use the {number} field") from exc
        if not rendered or len(rendered) > 100:
            raise ValueError("template must render to 1-100 characters")
        return value


class FileConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_name: str = "Ticket Bot"
    dev_guild_id: int
    bot_owner_ids: list[int]
    activity: dict[str, Any]
    embed_theme: EmbedTheme
    ticket_format: TicketFormat
    max_open_tickets_per_user: int = Field(ge=1, le=25)
    support_can_manage_messages: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    bot_name: str = "Ticket Bot"
    dev_guild_id: int
    discord_token: str = Field(min_length=20, repr=False)
    log_level: str = "INFO"
    db_path: Path
    transcript_path: Path
    log_path: Path
    bot_owner_ids: list[int]
    activity: dict[str, Any]
    embed_theme: EmbedTheme
    ticket_format: TicketFormat
    max_open_tickets_per_user: int
    support_can_manage_messages: bool

    @field_validator("discord_token")
    @classmethod
    def _token_not_placeholder(cls, value: str) -> str:
        if value == _PLACEHOLDER_TOKEN:
            raise ValueError("DISCORD_TOKEN still has the placeholder value")
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown log level '{value}'")
        return level

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        base_dir: Path = PROJECT_ROOT,
    ) -> Settings:
        environ = os.environ if env is None else env
        path = config_path or base_dir / "config.json"
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Config file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"{path.name} is not valid JSON: {exc}") from exc

        def resolve(value: str) -> Path:
            candidate = Path(value)
            return candidate if candidate.is_absolute() else (base_dir / candidate).resolve()

        try:
            file_cfg = FileConfig.model_validate(raw)
            return cls(
                bot_name=file_cfg.bot_name,
                discord_token=environ.get("DISCORD_TOKEN", ""),
                log_level=environ.get("LOG_LEVEL", "INFO"),
                db_path=resolve(environ.get("DB_PATH", "./db")),
                transcript_path=resolve(environ.get("TRANSCRIPT_PATH", "./assets/transcripts")),
                log_path=resolve(environ.get("LOG_PATH", "./logs")),
                dev_guild_id=file_cfg.dev_guild_id,
                bot_owner_ids=file_cfg.bot_owner_ids,
                activity=file_cfg.activity,
                embed_theme=file_cfg.embed_theme,
                ticket_format=file_cfg.ticket_format,
                max_open_tickets_per_user=file_cfg.max_open_tickets_per_user,
                support_can_manage_messages=file_cfg.support_can_manage_messages,
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise ConfigurationError(f"Invalid configuration: {problems}") from exc
