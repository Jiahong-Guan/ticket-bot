from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from tests.fakes import make_channel, make_interaction, make_member, make_role
from utils.database import Database, Panel, Ticket
from utils.exceptions import PanelNotFound, PermissionDenied, TicketLimitReached
from utils.views.panel_wizard import (
    TOTAL_STEPS,
    PanelNameModal,
    PanelWizardView,
    WizardState,
)
from utils.views.ticket_controls import (
    CLAIM_ID,
    CLOSE_ID,
    DELETE_ID,
    RENAME_ID,
    REOPEN_ID,
    ClosedControlView,
    DeleteTicketModal,
    TicketButtonView,
    TicketControlView,
)

SUPPORT_ROLE = 50


async def seed(db: Database) -> tuple[Panel, Ticket]:
    panel = await db.create_panel(
        guild_id=1,
        name="P",
        support_role_ids=[SUPPORT_ROLE],
        category_id=1,
        transcript_channel_id=2,
        panel_channel_id=3,
    )
    ticket = await db.create_ticket(
        guild_id=1, panel_id=panel.id, channel_id=500, owner_id=5, number=1
    )
    return panel, ticket


@pytest.mark.asyncio
async def test_persistent_views_have_stable_custom_ids(bot: SimpleNamespace) -> None:
    ids = {c.custom_id for c in TicketControlView(bot).children if hasattr(c, "custom_id")}  # type: ignore[arg-type]
    assert ids == {CLOSE_ID, CLAIM_ID, RENAME_ID}
    closed_ids = {c.custom_id for c in ClosedControlView(bot).children if hasattr(c, "custom_id")}  # type: ignore[arg-type]
    assert closed_ids == {REOPEN_ID, DELETE_ID}
    for view in (TicketControlView(bot), ClosedControlView(bot), TicketButtonView(bot, 9)):  # type: ignore[arg-type]
        assert view.is_persistent()


@pytest.mark.asyncio
async def test_ticket_button_uses_panel_id_in_custom_id(bot: SimpleNamespace) -> None:
    view = TicketButtonView(bot, 12, "Open one")  # type: ignore[arg-type]
    button = view.children[0]
    assert isinstance(button, discord.ui.Button)
    assert button.custom_id == "create_ticket:12"
    assert button.label == "Open one"


@pytest.mark.asyncio
async def test_create_button_creates_ticket_and_confirms_ephemerally(
    bot: SimpleNamespace, db: Database
) -> None:
    panel, ticket = await seed(db)
    channel = make_channel(700)
    bot.tickets = SimpleNamespace(create_ticket=AsyncMock(return_value=(ticket, channel)))
    view = TicketButtonView(bot, panel.id)  # type: ignore[arg-type]
    inter = make_interaction(user=make_member(5), guild=MagicMock(spec=discord.Guild), client=bot)
    inter.response.defer = AsyncMock()

    await view._on_click(inter)

    inter.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    sent = inter.followup.send.await_args.kwargs
    assert channel.mention in sent["embed"].description
    assert sent["ephemeral"] is True


@pytest.mark.asyncio
async def test_create_button_for_deleted_panel(bot: SimpleNamespace) -> None:
    view = TicketButtonView(bot, 404)  # type: ignore[arg-type]
    inter = make_interaction(user=make_member(5), guild=MagicMock(spec=discord.Guild), client=bot)
    inter.response.defer = AsyncMock()
    with pytest.raises(PanelNotFound):
        await view._on_click(inter)
    inter.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_button_propagates_limit_error_for_the_handler(
    bot: SimpleNamespace, db: Database
) -> None:
    panel, _ = await seed(db)
    bot.tickets = SimpleNamespace(
        create_ticket=AsyncMock(side_effect=TicketLimitReached("https://jump", 1))
    )
    view = TicketButtonView(bot, panel.id)  # type: ignore[arg-type]
    inter = make_interaction(user=make_member(5), guild=MagicMock(spec=discord.Guild), client=bot)
    inter.response.defer = AsyncMock()
    with pytest.raises(TicketLimitReached):
        await view._on_click(inter)


@pytest.mark.asyncio
async def test_close_button_blocks_strangers_before_showing_modal(
    bot: SimpleNamespace, db: Database
) -> None:
    await seed(db)
    view = TicketControlView(bot)  # type: ignore[arg-type]
    channel = make_channel(500)
    inter = make_interaction(user=make_member(99), guild=object(), client=bot)
    inter.channel = channel
    inter.response.send_modal = AsyncMock()
    with pytest.raises(PermissionDenied):
        await view.close_button.callback(inter)  # type: ignore[call-arg]
    inter.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_button_opens_modal_for_owner(bot: SimpleNamespace, db: Database) -> None:
    await seed(db)
    view = TicketControlView(bot)  # type: ignore[arg-type]
    inter = make_interaction(user=make_member(5), guild=object(), client=bot)
    inter.channel = make_channel(500)
    inter.response.send_modal = AsyncMock()
    await view.close_button.callback(inter)  # type: ignore[call-arg]
    inter.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_modal_requires_exact_confirmation(bot: SimpleNamespace, db: Database) -> None:
    await seed(db)
    bot.tickets = SimpleNamespace(delete_ticket=AsyncMock())
    modal = DeleteTicketModal(bot)  # type: ignore[arg-type]
    inter = make_interaction(
        user=make_member(10, roles=(SUPPORT_ROLE,)), guild=object(), client=bot
    )
    inter.channel = make_channel(500)

    modal.confirm._value = "please"
    await modal.on_submit(inter)
    bot.tickets.delete_ticket.assert_not_awaited()
    assert "did not match" in inter.response.send_message.await_args.kwargs["embed"].description

    modal.confirm._value = " delete "
    await modal.on_submit(inter)
    bot.tickets.delete_ticket.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_modal_denies_non_support_even_with_correct_text(
    bot: SimpleNamespace, db: Database
) -> None:
    await seed(db)
    bot.tickets = SimpleNamespace(delete_ticket=AsyncMock())
    modal = DeleteTicketModal(bot)  # type: ignore[arg-type]
    inter = make_interaction(user=make_member(5), guild=object(), client=bot)
    inter.channel = make_channel(500)
    modal.confirm._value = "DELETE"
    with pytest.raises(PermissionDenied):
        await modal.on_submit(inter)
    bot.tickets.delete_ticket.assert_not_awaited()


def guild_with_roles(*role_ids: int, default: int | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    roles = {rid: make_role(rid, default=rid == default) for rid in role_ids}
    guild.get_role.side_effect = roles.get
    return guild


@pytest.mark.asyncio
async def test_wizard_renders_the_right_component_per_step(bot: SimpleNamespace) -> None:
    view = PanelWizardView(bot, WizardState(owner_id=1, guild_id=1))  # type: ignore[arg-type]
    first = view.children
    assert any(isinstance(c, discord.ui.Button) and c.label == "Set name" for c in first)
    back = next(c for c in first if getattr(c, "label", "") == "<- Back")
    assert back.disabled is True  # type: ignore[union-attr]

    view.state.step = 2
    view._render()
    assert any(isinstance(c, discord.ui.RoleSelect) for c in view.children)
    assert not next(c for c in view.children if getattr(c, "label", "") == "<- Back").disabled  # type: ignore[union-attr]

    for step, channel_type in ((3, discord.ChannelType.category), (4, discord.ChannelType.text)):
        view.state.step = step
        view._render()
        select = next(c for c in view.children if isinstance(c, discord.ui.ChannelSelect))
        assert select.channel_types == [channel_type]

    view.state.step = TOTAL_STEPS
    view._render()
    assert any(getattr(c, "label", "") == "Finish" for c in view.children)
    assert not any(getattr(c, "label", "") == "Save & Continue" for c in view.children)


@pytest.mark.asyncio
async def test_wizard_embed_matches_step(bot: SimpleNamespace) -> None:
    state = WizardState(owner_id=1, guild_id=1, step=3, name="Billing", support_role_ids=[7])
    embed = PanelWizardView(bot, state).build_embed()  # type: ignore[arg-type]
    assert "step 3/5" in embed.title  # type: ignore[operator]
    value = embed.fields[0].value or ""
    assert "Billing" in value
    assert "<@&7>" in value


@pytest.mark.asyncio
async def test_wizard_validation_blocks_each_incomplete_step(bot: SimpleNamespace) -> None:
    view = PanelWizardView(bot, WizardState(owner_id=1, guild_id=1))  # type: ignore[arg-type]
    guild = guild_with_roles(7, 8, default=8)

    assert view._validate_step(guild) == "Set a panel name first."
    view.state.name = "P"
    assert view._validate_step(guild) is None

    view.state.step = 2
    assert "at least one" in (view._validate_step(guild) or "")
    view.state.support_role_ids = [8]
    assert "not @everyone" in (view._validate_step(guild) or "")
    view.state.support_role_ids = [999]  # role deleted meanwhile
    assert view._validate_step(guild) is not None
    view.state.support_role_ids = [7]
    assert view._validate_step(guild) is None

    for step, message in ((3, "category"), (4, "transcript"), (5, "panel channel")):
        view.state.step = step
        assert message in (view._validate_step(guild) or "")


@pytest.mark.asyncio
async def test_wizard_only_answers_its_owner(bot: SimpleNamespace) -> None:
    view = PanelWizardView(bot, WizardState(owner_id=1, guild_id=1))  # type: ignore[arg-type]
    intruder = make_interaction(user=SimpleNamespace(id=2), client=bot)
    assert await view.interaction_check(intruder) is False
    assert intruder.response.send_message.await_args.kwargs["ephemeral"] is True
    owner = make_interaction(user=SimpleNamespace(id=1), client=bot)
    assert await view.interaction_check(owner) is True


@pytest.mark.asyncio
async def test_wizard_finish_refuses_when_bot_lacks_permissions(bot: SimpleNamespace) -> None:
    state = WizardState(
        owner_id=1,
        guild_id=1,
        step=5,
        name="P",
        support_role_ids=[7],
        category_id=10,
        transcript_channel_id=20,
        panel_channel_id=30,
    )
    view = PanelWizardView(bot, state)  # type: ignore[arg-type]

    def channel(mention: str, **perms: bool) -> MagicMock:
        ch = MagicMock()
        ch.mention = mention
        ch.permissions_for.return_value = SimpleNamespace(
            **{
                p: perms.get(p, True)
                for p in (
                    "view_channel",
                    "manage_channels",
                    "manage_roles",
                    "send_messages",
                    "attach_files",
                    "embed_links",
                )
            }
        )
        return ch

    channels = {
        10: channel("#cat", manage_channels=False),
        20: channel("#tx"),
        30: channel("#panel", send_messages=False),
    }
    guild = guild_with_roles(7)
    guild.me = make_member(999)
    guild.get_channel.side_effect = channels.get

    problems = view._permission_problems(guild)
    assert len(problems) == 2
    assert any("manage channels" in p and "ticket category" in p for p in problems)
    assert any("send messages" in p and "panel channel" in p for p in problems)

    inter = make_interaction(user=SimpleNamespace(id=1), guild=guild, client=bot)
    inter.response.defer = AsyncMock()
    await view._finish(inter, guild)
    inter.response.defer.assert_not_awaited()
    assert "Missing bot permissions" in inter.response.send_message.await_args.kwargs["embed"].title
    assert await bot.db.list_panels() == []  # nothing was created


@pytest.mark.asyncio
async def test_wizard_finish_creates_panel_posts_message_and_updates_guild(
    bot: SimpleNamespace, db: Database
) -> None:
    state = WizardState(
        owner_id=1,
        guild_id=1,
        step=5,
        name="Help desk",
        support_role_ids=[7, 8],
        category_id=10,
        transcript_channel_id=20,
        panel_channel_id=30,
    )
    view = PanelWizardView(bot, state)  # type: ignore[arg-type]

    panel_channel = MagicMock(spec=discord.TextChannel)
    panel_channel.mention = "#panel"
    panel_channel.permissions_for.return_value = SimpleNamespace(
        **dict.fromkeys(
            (
                "view_channel",
                "manage_channels",
                "manage_roles",
                "send_messages",
                "attach_files",
                "embed_links",
            ),
            True,
        )
    )
    panel_channel.send = AsyncMock(return_value=SimpleNamespace(id=9001))
    guild = guild_with_roles(7, 8)
    guild.id = 1
    guild.me = make_member(999)
    guild.get_channel.side_effect = lambda cid: panel_channel
    inter = make_interaction(user=SimpleNamespace(id=1), guild=guild, client=bot)
    inter.response.defer = AsyncMock()
    inter.edit_original_response = AsyncMock()

    await view._finish(inter, guild)

    panels = await db.list_panels(1)
    assert len(panels) == 1
    assert panels[0].message_id == 9001
    assert panels[0].support_role_ids == [7, 8]
    sent = panel_channel.send.await_args.kwargs
    assert sent["embed"].title == "Support Tickets"
    assert sent["view"].children[0].custom_id == f"create_ticket:{panels[0].id}"
    cfg = await db.get_guild(1)
    assert cfg.support_roles == [7, 8]
    assert cfg.ticket_category == 10
    assert cfg.panel_message_id == 9001
    assert "Setup complete" in inter.edit_original_response.await_args.kwargs["embed"].title


@pytest.mark.asyncio
async def test_wizard_finish_rolls_back_panel_if_posting_fails(
    bot: SimpleNamespace, db: Database
) -> None:
    state = WizardState(
        owner_id=1,
        guild_id=1,
        step=5,
        name="P",
        support_role_ids=[7],
        category_id=10,
        transcript_channel_id=20,
        panel_channel_id=30,
    )
    view = PanelWizardView(bot, state)  # type: ignore[arg-type]
    panel_channel = MagicMock(spec=discord.TextChannel)
    panel_channel.permissions_for.return_value = SimpleNamespace(
        **dict.fromkeys(
            (
                "view_channel",
                "manage_channels",
                "manage_roles",
                "send_messages",
                "attach_files",
                "embed_links",
            ),
            True,
        )
    )
    panel_channel.send = AsyncMock(
        side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "no",  # type: ignore[arg-type]
        )
    )
    guild = guild_with_roles(7)
    guild.id = 1
    guild.me = make_member(999)
    guild.get_channel.side_effect = lambda cid: panel_channel
    inter = make_interaction(user=SimpleNamespace(id=1), guild=guild, client=bot)
    inter.response.defer = AsyncMock()

    with pytest.raises(discord.Forbidden):
        await view._finish(inter, guild)
    assert await db.list_panels() == []  # no orphan panel row


@pytest.mark.asyncio
async def test_panel_name_modal_stores_trimmed_name_and_rejects_blank(
    bot: SimpleNamespace,
) -> None:
    view = PanelWizardView(bot, WizardState(owner_id=1, guild_id=1))  # type: ignore[arg-type]
    modal = PanelNameModal(view)
    inter = make_interaction(user=SimpleNamespace(id=1), client=bot)
    inter.response.edit_message = AsyncMock()

    modal.name._value = "   "
    await modal.on_submit(inter)
    assert view.state.name is None
    inter.response.edit_message.assert_not_awaited()

    modal.name._value = "  Billing  "
    await modal.on_submit(inter)
    assert view.state.name == "Billing"
    inter.response.edit_message.assert_awaited_once()
