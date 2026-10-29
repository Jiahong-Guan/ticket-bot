from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import discord

from tests.fakes import make_channel, make_member, make_role
from utils.config import Settings
from utils.database import Database, GuildSettings, Panel, TicketStatus
from utils.exceptions import ConfigurationError, TicketLimitReached
from utils.ticket_service import TicketService

PANEL_ROLE = 50
GUILD_ROLE = 51


@pytest_asyncio.fixture
async def panel(db: Database) -> Panel:
    await db.upsert_guild(GuildSettings(guild_id=1, support_roles=[GUILD_ROLE]))
    return await db.create_panel(
        guild_id=1,
        name="Support",
        support_role_ids=[PANEL_ROLE],
        category_id=10,
        transcript_channel_id=20,
        panel_channel_id=30,
    )


def make_guild(new_channel: MagicMock, *, category: object | None = "ok") -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.default_role = make_role(1, default=True)
    guild.me = make_member(999)
    category_obj = MagicMock(spec=discord.CategoryChannel) if category == "ok" else category
    guild.get_channel.side_effect = lambda cid: category_obj if cid == 10 else None
    roles = {PANEL_ROLE: make_role(PANEL_ROLE), GUILD_ROLE: make_role(GUILD_ROLE)}
    guild.get_role.side_effect = roles.get
    guild.create_text_channel = AsyncMock(return_value=new_channel)
    return guild


def make_new_channel() -> MagicMock:
    channel = make_channel(700)
    welcome = SimpleNamespace(id=4242, pin=AsyncMock())
    channel.send = AsyncMock(return_value=welcome)
    channel.delete = AsyncMock()
    return channel


@pytest.mark.asyncio
async def test_create_ticket_happy_path(bot: SimpleNamespace, db: Database, panel: Panel) -> None:
    service = TicketService(bot)  # type: ignore[arg-type]
    channel = make_new_channel()
    guild = make_guild(channel)
    owner = make_member(5)

    ticket, created = await service.create_ticket(guild, owner, panel)  # type: ignore[arg-type]

    assert created is channel
    assert guild.create_text_channel.await_args.args[0] == "ticket-0001"
    kwargs = guild.create_text_channel.await_args.kwargs
    overwrites = kwargs["overwrites"]
    assert overwrites[guild.default_role].view_channel is False
    assert overwrites[owner].send_messages is True
    assert overwrites[owner].manage_messages is None  # owners get no moderation rights
    assert overwrites[guild.me].manage_channels is True
    support_overwrites = [
        ow
        for target, ow in overwrites.items()
        if target not in (guild.default_role, owner, guild.me)
    ]
    assert len(support_overwrites) == 2  # panel role + guild-level role
    assert all(ow.manage_messages is True for ow in support_overwrites)
    assert "Owner ID: 5" in kwargs["topic"]

    welcome = channel.send.await_args.kwargs
    content = welcome["content"]
    assert content.startswith(owner.mention)
    assert f"<@&{PANEL_ROLE}>" in content
    assert f"<@&{GUILD_ROLE}>" in content
    assert welcome["embed"].title == "Ticket #0001"

    allowed = welcome["allowed_mentions"]
    assert owner in allowed.users
    assert {r.id for r in allowed.roles} == {PANEL_ROLE, GUILD_ROLE}

    channel.send.return_value.pin.assert_awaited_once()

    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.channel_id == 700
    assert stored.owner_id == 5
    assert stored.number == 1
    assert stored.control_message_id == 4242
    assert stored.status is TicketStatus.OPEN


@pytest.mark.asyncio
async def test_support_manage_messages_can_be_disabled(
    bot: SimpleNamespace, settings: Settings, panel: Panel
) -> None:
    bot.settings = settings.model_copy(update={"support_can_manage_messages": False})
    service = TicketService(bot)  # type: ignore[arg-type]
    guild = make_guild(make_new_channel())
    owner = make_member(5)
    await service.create_ticket(guild, owner, panel)  # type: ignore[arg-type]
    overwrites = guild.create_text_channel.await_args.kwargs["overwrites"]
    support = [ow for t, ow in overwrites.items() if t not in (guild.default_role, owner, guild.me)]
    assert all(ow.manage_messages is None for ow in support)


@pytest.mark.asyncio
async def test_ticket_numbers_increase(bot: SimpleNamespace, panel: Panel) -> None:
    service = TicketService(bot)  # type: ignore[arg-type]
    for expected, owner_id in enumerate((5, 6, 7), start=1):
        guild = make_guild(make_new_channel())
        guild.get_channel.side_effect = lambda cid: (
            MagicMock(spec=discord.CategoryChannel) if cid == 10 else make_channel(cid)
        )
        ticket, _ = await service.create_ticket(guild, make_member(owner_id), panel)  # type: ignore[arg-type]
        assert ticket.number == expected
        assert guild.create_text_channel.await_args.args[0] == f"ticket-{expected:04d}"


@pytest.mark.asyncio
async def test_second_ticket_is_refused_with_jump_link(bot: SimpleNamespace, panel: Panel) -> None:
    service = TicketService(bot)  # type: ignore[arg-type]
    first_channel = make_new_channel()
    guild = make_guild(first_channel)
    owner = make_member(5)
    await service.create_ticket(guild, owner, panel)  # type: ignore[arg-type]

    guild.get_channel.side_effect = lambda cid: (
        MagicMock(spec=discord.CategoryChannel) if cid == 10 else first_channel
    )
    with pytest.raises(TicketLimitReached) as caught:
        await service.create_ticket(guild, owner, panel)  # type: ignore[arg-type]
    assert first_channel.jump_url in caught.value.user_message
    assert guild.create_text_channel.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_clicks_create_only_one_ticket(bot: SimpleNamespace, panel: Panel) -> None:
    import asyncio

    service = TicketService(bot)  # type: ignore[arg-type]
    channel = make_new_channel()
    guild = make_guild(channel)
    guild.get_channel.side_effect = lambda cid: (
        MagicMock(spec=discord.CategoryChannel) if cid == 10 else channel
    )
    owner = make_member(5)
    results = await asyncio.gather(
        *(service.create_ticket(guild, owner, panel) for _ in range(5)),  # type: ignore[arg-type]
        return_exceptions=True,
    )
    assert sum(1 for r in results if isinstance(r, tuple)) == 1
    assert sum(1 for r in results if isinstance(r, TicketLimitReached)) == 4
    assert guild.create_text_channel.await_count == 1


@pytest.mark.asyncio
async def test_missing_category_raises_configuration_error(
    bot: SimpleNamespace, panel: Panel
) -> None:
    service = TicketService(bot)  # type: ignore[arg-type]
    guild = make_guild(make_new_channel(), category=None)
    with pytest.raises(ConfigurationError, match="category"):
        await service.create_ticket(guild, make_member(5), panel)  # type: ignore[arg-type]
    guild.create_text_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_is_cleaned_up_if_the_db_write_fails(
    bot: SimpleNamespace, db: Database, panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TicketService(bot)  # type: ignore[arg-type]
    channel = make_new_channel()
    guild = make_guild(channel)

    async def broken(**_: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "create_ticket", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        await service.create_ticket(guild, make_member(5), panel)  # type: ignore[arg-type]
    channel.delete.assert_awaited_once()
    assert await db.list_tickets() == []


@pytest.mark.asyncio
async def test_pin_failure_does_not_fail_ticket_creation(
    bot: SimpleNamespace, panel: Panel
) -> None:
    service = TicketService(bot)  # type: ignore[arg-type]
    channel = make_new_channel()
    channel.send.return_value.pin = AsyncMock(
        side_effect=discord.HTTPException(
            response=SimpleNamespace(status=403, reason="Forbidden"),
            message="no pin",  # type: ignore[arg-type]
        )
    )
    ticket, _ = await service.create_ticket(make_guild(channel), make_member(5), panel)  # type: ignore[arg-type]
    assert ticket.number == 1
