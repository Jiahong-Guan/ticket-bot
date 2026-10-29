from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import discord
from discord import app_commands

import main as main_module
from bot import TicketBot
from cogs.general import collect_commands, format_uptime
from utils.config import Settings
from utils.logger import get_logger, setup_logging

EXPECTED = {
    "add", "remove", "open", "close", "delete", "rename", "claim",
    "setup", "ping", "help", "debug", "permissionlevel", "id", "admin",
}  # fmt: skip


@pytest_asyncio.fixture
async def loaded_bot(settings: Settings) -> TicketBot:
    bot = TicketBot(settings)
    # noinspection PyProtectedMember
    await bot._load_cogs()
    return bot


@pytest.mark.asyncio
async def test_every_cog_loads_and_registers_its_commands(loaded_bot: TicketBot) -> None:
    assert loaded_bot.failed_cogs == []
    assert sorted(loaded_bot.loaded_cogs) == [
        "cogs.admin", "cogs.general", "cogs.panels", "cogs.tickets",
    ]  # fmt: skip
    assert {c.name for c in loaded_bot.tree.get_commands()} == EXPECTED


@pytest.mark.asyncio
async def test_base_command_is_not_loaded_as_a_cog(loaded_bot: TicketBot) -> None:
    assert "cogs.base_command" not in loaded_bot.loaded_cogs
    assert "cogs.__init__" not in loaded_bot.loaded_cogs


@pytest.mark.asyncio
async def test_every_cog_inherits_basecog(loaded_bot: TicketBot) -> None:
    from cogs.base_command import BaseCog

    assert loaded_bot.cogs
    assert all(isinstance(cog, BaseCog) for cog in loaded_bot.cogs.values())


@pytest.mark.asyncio
async def test_every_command_has_a_description_and_documented_params(
    loaded_bot: TicketBot,
) -> None:
    def leaves(cmds: list) -> list[app_commands.Command]:  # type: ignore[type-arg]
        out = []
        for c in cmds:
            out.extend(leaves(list(c.commands)) if isinstance(c, app_commands.Group) else [c])
        return out

    for command in leaves(loaded_bot.tree.get_commands()):
        assert command.description and command.description != "…", command.name
        for param in command.parameters:
            assert param.description and param.description != "…", f"{command.name}.{param.name}"


@pytest.mark.asyncio
async def test_help_is_built_from_the_live_tree(loaded_bot: TicketBot) -> None:
    rows = collect_commands(loaded_bot.tree.get_commands())
    usages = {usage for _, usage, _ in rows}
    assert {"/add", "/close", "/setup", "/admin config", "/admin delete-panel"} <= usages
    sections = {section for section, _, _ in rows}
    assert {"TicketsCog", "GeneralCog", "PanelsCog", "AdminCog"} <= sections


@pytest.mark.asyncio
async def test_persistent_views_are_reregistered_from_the_database(
    loaded_bot: TicketBot,
) -> None:
    await loaded_bot.db.open()
    panel = await loaded_bot.db.create_panel(
        guild_id=1, name="P", support_role_ids=[1], category_id=2,
        transcript_channel_id=3, panel_channel_id=4, button_label="Open",
    )  # fmt: skip
    await loaded_bot.db.update_panel(type(panel)(**{**panel.to_doc(), "message_id": 555}))
    await loaded_bot.db.create_panel(
        guild_id=1, name="unposted", support_role_ids=[1], category_id=2,
        transcript_channel_id=3, panel_channel_id=4,
    )  # fmt: skip

    await loaded_bot._register_persistent_views()

    ids = {
        child.custom_id
        for view in loaded_bot.persistent_views
        for child in view.children
        if isinstance(child, discord.ui.Button)
    }
    assert f"create_ticket:{panel.id}" in ids
    assert len([i for i in ids if i.startswith("create_ticket:")]) == 1  # unposted panel skipped
    assert {"ticket_controls:close", "ticket_closed:reopen"} <= ids
    await loaded_bot.db.close()


@pytest.mark.asyncio
async def test_bot_requests_the_intents_it_needs(settings: Settings) -> None:
    bot = TicketBot(settings)
    assert bot.intents.guilds
    assert bot.intents.members
    assert bot.intents.message_content  # transcripts need message text
    assert not bot.intents.presences
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.roles is False


def test_format_uptime() -> None:
    assert format_uptime(0) == "0d 0h 0m 0s"
    assert format_uptime(90061) == "1d 1h 1m 1s"


def test_logging_creates_both_files_and_splits_levels(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        setup_logging("INFO", tmp_path / "logs")
        log = get_logger("unit")
        log.info("just info")
        log.error("a real problem")
        for handler in logging.getLogger().handlers:
            handler.flush()
        bot_log = (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")
        err_log = (tmp_path / "logs" / "error.log").read_text(encoding="utf-8")
        assert "just info" in bot_log
        assert "a real problem" in bot_log
        assert "just info" not in err_log
        assert "a real problem" in err_log

        error_lines = [ln for ln in bot_log.splitlines() if "a real problem" in ln]
        assert len(error_lines) == 1
        line = error_lines[0]
        assert "| ERROR" in line
        assert "ticketbot.unit" in line
        assert line.endswith("| a real problem")
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in saved:
            root.addHandler(handler)


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        setup_logging("INFO", tmp_path)
        setup_logging("INFO", tmp_path)
        assert len(root.handlers) == 3
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in saved:
            root.addHandler(handler)


@pytest.fixture
def restore_logging() -> None:
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    yield  # type: ignore[misc]
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(level)


def test_main_exits_2_on_bad_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "")
    assert main_module.main() == 2
    assert "Configuration error" in capsys.readouterr().err


def run_main_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException | None
) -> int:
    monkeypatch.setenv("DISCORD_TOKEN", "x" * 40)
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "logs"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db"))

    class FakeBot:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        @staticmethod
        def run(token: str, **_: object) -> None:
            assert token == "x" * 40
            if error:
                raise error

    monkeypatch.setattr(main_module, "TicketBot", FakeBot)
    return main_module.main()


@pytest.mark.usefixtures("restore_logging")
def test_main_success_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert run_main_with(monkeypatch, tmp_path, None) == 0


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(
    "error",
    [
        discord.LoginFailure("bad token"),
        discord.PrivilegedIntentsRequired(None),
        RuntimeError("boom"),
    ],
)
def test_main_returns_nonzero_and_logs_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    assert run_main_with(monkeypatch, tmp_path, error) == 1
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "Traceback" in (tmp_path / "logs" / "error.log").read_text(encoding="utf-8")


def test_settings_stand_in(settings: Settings) -> None:
    assert SimpleNamespace(settings=settings).settings.max_open_tickets_per_user == 1


@pytest.mark.asyncio
async def test_setup_hook_opens_db_registers_views_loads_cogs_and_syncs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    bot = TicketBot(settings)
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[1, 2, 3]))
    await bot.setup_hook()
    assert bot.synced_commands == 3
    assert bot.failed_cogs == []
    assert settings.db_path.exists()
    assert len(bot.persistent_views) >= 2
    await bot.db.close()


@pytest.mark.asyncio
async def test_a_failed_sync_does_not_stop_startup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    bot = TicketBot(settings)
    failure = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")  # type: ignore[arg-type]
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(side_effect=failure))
    await bot.setup_hook()
    assert bot.synced_commands == 0
    await bot.db.close()


@pytest.mark.asyncio
async def test_a_broken_cog_is_logged_and_skipped(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = TicketBot(settings)
    real = bot.load_extension

    async def flaky(name: str, **kw: object) -> None:
        if name == "cogs.panels":
            raise RuntimeError("bad cog")
        await real(name, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(bot, "load_extension", flaky)
    await bot._load_cogs()
    assert bot.failed_cogs == ["cogs.panels"]
    assert "cogs.tickets" in bot.loaded_cogs  # the rest still loaded


@pytest.mark.asyncio
async def test_tree_errors_reach_the_central_handler(settings: Settings) -> None:
    from tests.fakes import make_interaction, make_member

    bot = TicketBot(settings)
    inter = make_interaction(user=make_member(1), client=bot)
    await bot.tree.on_error(inter, app_commands.CheckFailure("nope"))
    assert "nope" in inter.response.send_message.await_args.kwargs["embed"].description
