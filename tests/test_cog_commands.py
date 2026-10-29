from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from cogs.admin import AdminCog
from cogs.general import GeneralCog
from cogs.panels import PanelsCog
from cogs.tickets import TicketsCog
from tests.fakes import make_channel, make_interaction, make_member, make_role
from utils.config import Settings
from utils.database import Database, GuildSettings, Ticket
from utils.exceptions import BotError, TicketNotFound
from utils.views.ticket_controls import DeleteTicketModal

SUPPORT_ROLE = 50


@pytest.fixture
def service() -> SimpleNamespace:
    return SimpleNamespace(
        add_targets=AsyncMock(return_value=["<@30>"]),
        remove_targets=AsyncMock(return_value=["<@30>"]),
        reopen_ticket=AsyncMock(return_value=SimpleNamespace(renamed=True)),
        close_via_interaction=AsyncMock(),
        rename_ticket=AsyncMock(return_value="billing"),
        toggle_claim=AsyncMock(return_value=SimpleNamespace(claimed_by=10)),
    )


@pytest.fixture
def cog_bot(bot: SimpleNamespace, service: SimpleNamespace, settings: Settings) -> SimpleNamespace:
    bot.tickets = service
    bot.latency = 0.042
    bot.guilds = [1, 2]
    bot.loaded_cogs = ["a", "b"]
    bot.failed_cogs = []
    bot.synced_commands = 14
    bot.started_at = discord.utils.utcnow()
    bot.tree = SimpleNamespace(get_commands=lambda: [])
    return bot


async def ticket_in(db: Database, channel_id: int = 500) -> Ticket:
    panel = await db.create_panel(
        guild_id=1, name="P", support_role_ids=[SUPPORT_ROLE], category_id=1,
        transcript_channel_id=2, panel_channel_id=3,
    )  # fmt: skip
    return await db.create_ticket(
        guild_id=1, panel_id=panel.id, channel_id=channel_id, owner_id=5, number=1
    )


def inter_in(bot: Any, channel: Any, user: Any = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.return_value = None
    inter = make_interaction(
        user=user or make_member(10, roles=(SUPPORT_ROLE,)), guild=guild, client=bot,
        channel_id=channel.id,
    )  # fmt: skip
    inter.channel = channel
    inter.response.defer = AsyncMock()
    inter.response.send_modal = AsyncMock()
    return inter


def embed_of(call: Any) -> discord.Embed:
    return call.kwargs["embed"]  # type: ignore[no-any-return]


# TICKET COGS
@pytest.mark.asyncio
async def test_add_in_current_channel_is_public(cog_bot: Any, db: Database, service: Any) -> None:
    await ticket_in(db)
    cog = TicketsCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.add.callback(cog, inter, user=make_member(30))  # type: ignore[call-arg]
    inter.response.defer.assert_awaited_once_with(ephemeral=False)
    assert "<@30>" in embed_of(inter.followup.send.await_args).description
    service.add_targets.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_to_another_ticket_is_ephemeral(cog_bot: Any, db: Database) -> None:
    await ticket_in(db, channel_id=600)
    cog = TicketsCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.add.callback(
        cog, inter, user=None, role=make_role(60), ticket=make_channel(600)
    )  # type: ignore[call-arg]
    inter.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_add_outside_a_ticket_fails_cleanly(cog_bot: Any, service: Any) -> None:
    cog = TicketsCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(999))
    with pytest.raises(TicketNotFound):
        await TicketsCog.add.callback(cog, inter, user=make_member(30))  # type: ignore[call-arg]
    service.add_targets.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_reports_revoke(cog_bot: Any, db: Database, service: Any) -> None:
    await ticket_in(db)
    cog = TicketsCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.remove.callback(cog, inter, user=make_member(30), revoke=True)  # type: ignore[call-arg]
    assert service.remove_targets.await_args.kwargs["revoke"] is True
    assert "blocked from being re-added" in embed_of(inter.followup.send.await_args).description


@pytest.mark.asyncio
async def test_open_close_rename_claim_delegate_to_the_service(
        cog_bot: Any, db: Database, service: Any
) -> None:
    await ticket_in(db)
    cog = TicketsCog(cog_bot)

    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.open.callback(cog, inter)  # type: ignore[call-arg]
    assert "reopened" in embed_of(inter.followup.send.await_args).description

    service.reopen_ticket.return_value = SimpleNamespace(renamed=False)
    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.open.callback(cog, inter)  # type: ignore[call-arg]
    assert "rate limit" in embed_of(inter.followup.send.await_args).description

    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.close.callback(cog, inter, reason="done")  # type: ignore[call-arg]
    inter.response.defer.assert_awaited_once()
    assert service.close_via_interaction.await_args.args[2] == "done"

    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.rename.callback(cog, inter, name="billing")  # type: ignore[call-arg]
    assert "`billing`" in embed_of(inter.followup.send.await_args).description

    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.claim.callback(cog, inter, user=None, reason="mine")  # type: ignore[call-arg]
    embed = embed_of(inter.followup.send.await_args)
    assert "Claimed by <@10>" in embed.description
    assert embed.fields[0].value == "mine"

    service.toggle_claim.return_value = SimpleNamespace(claimed_by=None)
    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.claim.callback(cog, inter, user=None, reason=None)  # type: ignore[call-arg]
    assert "no longer claimed" in embed_of(inter.followup.send.await_args).description


@pytest.mark.asyncio
async def test_delete_opens_confirmation_modal_instead_of_deleting(cog_bot: Any) -> None:
    cog = TicketsCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500))
    await TicketsCog.delete.callback(cog, inter)  # type: ignore[call-arg]
    assert isinstance(inter.response.send_modal.await_args.args[0], DeleteTicketModal)


# PANELS COGS
@pytest.mark.asyncio
async def test_setup_opens_wizard(cog_bot: Any) -> None:
    cog = PanelsCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500), user=make_member(1, manage_guild=True))
    await PanelsCog.setup_panel.callback(cog, inter)  # type: ignore[call-arg]
    sent = inter.response.send_message.await_args.kwargs
    assert "step 1/5" in sent["embed"].title
    assert sent["view"].state.owner_id == 1


# ADMIN COGS
@pytest.mark.asyncio
async def test_support_role_add_remove_and_everyone_guard(cog_bot: Any, db: Database) -> None:
    cog = AdminCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500))
    await AdminCog.add_support_role.callback(cog, inter, role=make_role(7))  # type: ignore[call-arg]
    await AdminCog.add_support_role.callback(cog, inter, role=make_role(8))  # type: ignore[call-arg]
    await AdminCog.add_support_role.callback(cog, inter, role=make_role(7))  # idempotent  # type: ignore[call-arg]
    assert (await db.get_guild(1)).support_roles == [7, 8]
    await AdminCog.remove_support_role.callback(cog, inter, role=make_role(7))  # type: ignore[call-arg]
    assert (await db.get_guild(1)).support_roles == [8]
    with pytest.raises(BotError, match="everyone"):
        await AdminCog.add_support_role.callback(cog, inter, role=make_role(1, default=True))  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_closed_category_set_and_clear(cog_bot: Any, db: Database) -> None:
    cog = AdminCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500))
    category = MagicMock(spec=discord.CategoryChannel)
    category.id, category.name = 77, "Closed"
    await AdminCog.closed_category.callback(cog, inter, category=category)  # type: ignore[call-arg]
    assert (await db.get_guild(1)).closed_category == 77
    await AdminCog.closed_category.callback(cog, inter, category=None)  # type: ignore[call-arg]
    assert (await db.get_guild(1)).closed_category is None


@pytest.mark.asyncio
async def test_show_config_and_list_panels(cog_bot: Any, db: Database) -> None:
    cog = AdminCog(cog_bot)
    inter = inter_in(cog_bot, make_channel(500))
    await AdminCog.list_panels.callback(cog, inter)  # type: ignore[call-arg]
    assert "No panels yet" in embed_of(inter.response.send_message.await_args).description

    await db.create_panel(
        guild_id=1,
        name="P",
        support_role_ids=[],
        category_id=1,
        transcript_channel_id=2,
        panel_channel_id=3,
    )
    await db.upsert_guild(GuildSettings(guild_id=1, support_roles=[9], ticket_category=4))
    inter = inter_in(cog_bot, make_channel(500))
    await AdminCog.list_panels.callback(cog, inter)  # type: ignore[call-arg]
    assert "**P**" in embed_of(inter.response.send_message.await_args).description

    inter = inter_in(cog_bot, make_channel(500))
    await AdminCog.show_config.callback(cog, inter)  # type: ignore[call-arg]
    values = {f.name: f.value for f in embed_of(inter.response.send_message.await_args).fields}
    assert values["Support roles"] == "<@&9>"
    assert values["Panels"] == "1"
    assert values["Tickets created"] == "0"


@pytest.mark.asyncio
async def test_delete_panel_removes_message_and_row_but_not_other_guilds(
    cog_bot: Any, db: Database
) -> None:
    cog = AdminCog(cog_bot)
    panel = await db.create_panel(
        guild_id=1, name="P", support_role_ids=[1], category_id=1,
        transcript_channel_id=2, panel_channel_id=3,
    )  # fmt: skip
    other = await db.create_panel(
        guild_id=2, name="Other", support_role_ids=[1], category_id=1,
        transcript_channel_id=2, panel_channel_id=3,
    )  # fmt: skip
    from dataclasses import replace

    await db.update_panel(replace(panel, message_id=900))

    inter = inter_in(cog_bot, make_channel(500))
    channel = MagicMock(spec=discord.TextChannel)
    partial = MagicMock()
    partial.delete = AsyncMock()
    channel.get_partial_message.return_value = partial
    inter.guild.get_channel.return_value = channel

    with pytest.raises(BotError, match="No panel"):
        await AdminCog.delete_panel.callback(cog, inter, panel_id=other.id)  # type: ignore[call-arg]
    assert await db.get_panel(other.id) is not None

    await AdminCog.delete_panel.callback(cog, inter, panel_id=panel.id)  # type: ignore[call-arg]
    partial.delete.assert_awaited_once()
    assert await db.get_panel(panel.id) is None


# GENERAL COGS
@pytest.mark.asyncio
async def test_general_commands_reply_ephemerally_with_embeds(cog_bot: Any, db: Database) -> None:
    cog = GeneralCog(cog_bot)
    await db.upsert_guild(GuildSettings(guild_id=1, support_roles=[SUPPORT_ROLE]))

    async def run(command: Any, **kwargs: Any) -> MagicMock:
        inter = inter_in(cog_bot, make_channel(999), user=make_member(10, roles=(SUPPORT_ROLE,)))
        await command.callback(cog, inter, **kwargs)
        sent = inter.response.send_message.await_args.kwargs
        assert sent["ephemeral"] is True
        assert isinstance(sent["embed"], discord.Embed)
        return inter

    inter = await run(GeneralCog.ping)
    assert "42 ms" in embed_of(inter.response.send_message.await_args).description

    await run(GeneralCog.help)

    inter = await run(GeneralCog.debug)
    names = {f.name for f in embed_of(inter.response.send_message.await_args).fields}
    assert {"Bot version", "Python", "Uptime", "Cogs", "Synced commands"} <= names

    inter = await run(GeneralCog.permissionlevel)
    assert "Support" in embed_of(inter.response.send_message.await_args).description
    inter = await run(GeneralCog.permissionlevel, user=make_member(42))
    assert "Owner" in embed_of(inter.response.send_message.await_args).description

    inter = await run(GeneralCog.id)
    assert {f.name for f in embed_of(inter.response.send_message.await_args).fields} == {
        "You", "Server", "This channel",
    }  # fmt: skip
    user = SimpleNamespace(display_name="Zed", id=123)
    inter = await run(GeneralCog.id, user=user, role=make_role(7), channel=make_channel(8))
    assert len(embed_of(inter.response.send_message.await_args).fields) == 3
