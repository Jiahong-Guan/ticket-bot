from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from discord import app_commands

from tests.fakes import make_interaction, make_member
from utils import checks
from utils.checks import PermissionLevel
from utils.database import Database, GuildSettings


def predicate_of(decorator: Callable[[Any], Any]) -> Callable[..., Any]:
    async def command() -> None: ...

    decorated = decorator(command)
    return decorated.__discord_app_commands_checks__[0]  # type: ignore[no-any-return]


async def setup_ticket(db: Database, *, channel_id: int = 500, owner: int = 5):  # type: ignore[no-untyped-def]
    panel = await db.create_panel(
        guild_id=1,
        name="P",
        support_role_ids=[77],
        category_id=1,
        transcript_channel_id=2,
        panel_channel_id=3,
    )
    return await db.create_ticket(
        guild_id=1, panel_id=panel.id, channel_id=channel_id, owner_id=owner, number=1
    )


@pytest.mark.asyncio
async def test_is_admin_requires_manage_guild(bot: SimpleNamespace) -> None:
    check = predicate_of(checks.is_admin())
    ok = make_interaction(user=make_member(1, manage_guild=True), guild=object(), client=bot)
    assert await check(ok) is True
    no = make_interaction(user=make_member(2), guild=object(), client=bot)
    with pytest.raises(app_commands.CheckFailure, match="Manage Server"):
        await check(no)


@pytest.mark.asyncio
async def test_checks_reject_dms(bot: SimpleNamespace) -> None:
    check = predicate_of(checks.is_admin())
    dm = make_interaction(user=object(), guild=None, client=bot)
    with pytest.raises(app_commands.NoPrivateMessage):
        await check(dm)


@pytest.mark.asyncio
async def test_is_support_uses_db_roles_not_hardcoded(bot: SimpleNamespace, db: Database) -> None:
    check = predicate_of(checks.is_support())
    member = make_member(3, roles=(55,))
    inter = make_interaction(user=member, guild=object(), client=bot, channel_id=None)
    with pytest.raises(app_commands.CheckFailure, match="support role"):
        await check(inter)

    await db.upsert_guild(GuildSettings(guild_id=1, support_roles=[55]))
    assert await check(inter) is True


@pytest.mark.asyncio
async def test_is_support_honours_panel_roles_inside_ticket(
    bot: SimpleNamespace, db: Database
) -> None:
    await setup_ticket(db)
    check = predicate_of(checks.is_support())
    panel_support = make_member(3, roles=(77,))
    inside = make_interaction(user=panel_support, guild=object(), client=bot, channel_id=500)
    assert await check(inside) is True
    #  same member is not support outside that ticket
    outside = make_interaction(user=panel_support, guild=object(), client=bot, channel_id=999)
    with pytest.raises(app_commands.CheckFailure):
        await check(outside)


@pytest.mark.asyncio
async def test_is_support_lets_admins_through(bot: SimpleNamespace) -> None:
    check = predicate_of(checks.is_support())
    inter = make_interaction(
        user=make_member(1, manage_guild=True), guild=object(), client=bot, channel_id=None
    )
    assert await check(inter) is True


@pytest.mark.asyncio
async def test_in_ticket(bot: SimpleNamespace, db: Database) -> None:
    await setup_ticket(db)
    check = predicate_of(checks.in_ticket())
    assert await check(make_interaction(user=make_member(1), guild=object(), client=bot)) is True
    with pytest.raises(app_commands.CheckFailure, match="ticket channel"):
        await check(make_interaction(user=make_member(1), guild=object(), client=bot, channel_id=1))
    with pytest.raises(app_commands.CheckFailure):
        await check(
            make_interaction(user=make_member(1), guild=object(), client=bot, channel_id=None)
        )


@pytest.mark.asyncio
async def test_in_ticket_ignores_deleted_tickets(bot: SimpleNamespace, db: Database) -> None:
    from dataclasses import replace

    from utils.database import TicketStatus

    ticket = await setup_ticket(db)
    await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.DELETED))
    check = predicate_of(checks.in_ticket())
    with pytest.raises(app_commands.CheckFailure):
        await check(make_interaction(user=make_member(1), guild=object(), client=bot))


@pytest.mark.asyncio
async def test_owner_or_support(bot: SimpleNamespace, db: Database) -> None:
    await setup_ticket(db, owner=5)
    check = predicate_of(checks.is_ticket_owner_or_support())
    owner = make_interaction(user=make_member(5), guild=object(), client=bot)
    support = make_interaction(user=make_member(6, roles=(77,)), guild=object(), client=bot)
    stranger = make_interaction(user=make_member(7), guild=object(), client=bot)
    assert await check(owner) is True
    assert await check(support) is True
    with pytest.raises(app_commands.CheckFailure, match="owner or support"):
        await check(stranger)
    outside = make_interaction(user=make_member(5), guild=object(), client=bot, channel_id=1)
    with pytest.raises(app_commands.CheckFailure, match="ticket channel"):
        await check(outside)


@pytest.mark.asyncio
async def test_bot_owner(bot: SimpleNamespace) -> None:
    check = predicate_of(checks.is_bot_owner())
    assert await check(make_interaction(user=SimpleNamespace(id=42), client=bot)) is True
    with pytest.raises(app_commands.CheckFailure, match="bot owner"):
        await check(make_interaction(user=SimpleNamespace(id=43), client=bot))


@pytest.mark.asyncio
async def test_check_all_stacks_every_decorator() -> None:
    async def command() -> None: ...

    decorated = checks.check_all(checks.is_support(), checks.in_ticket(), checks.is_admin())(
        command
    )
    assert len(decorated.__discord_app_commands_checks__) == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_permission_levels(bot: SimpleNamespace, db: Database) -> None:
    await db.upsert_guild(GuildSettings(guild_id=1, support_roles=[9]))
    assert await checks.get_permission_level(bot, make_member(42)) is PermissionLevel.OWNER  # type: ignore[arg-type]
    assert (
        await checks.get_permission_level(bot, make_member(1, manage_guild=True))  # type: ignore[arg-type]
        is PermissionLevel.ADMIN
    )
    assert (
        await checks.get_permission_level(bot, make_member(2, roles=(9,)))   # type: ignore
        is PermissionLevel.SUPPORT
    )  # type: ignore[arg-type]
    assert await checks.get_permission_level(bot, make_member(3)) is PermissionLevel.USER  # type: ignore[arg-type]
    assert (
        PermissionLevel.OWNER
        > PermissionLevel.ADMIN
        > PermissionLevel.SUPPORT
        > PermissionLevel.USER
    )
