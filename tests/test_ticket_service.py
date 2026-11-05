from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import discord

from tests.fakes import async_iter, make_channel, make_member, make_message, make_role
from utils.database import Database, GuildSettings, Ticket, TicketStatus
from utils.exceptions import (
    PermissionDenied,
    TargetBlocked,
    TicketLimitReached,
    TicketStateError,
)
from utils.rate_limit import ChannelEditLimiter
from utils.ticket_service import (
    TicketService,
    is_locked,
    lock_overwrites,
    sanitize_channel_name,
    unlock_overwrites,
)

SUPPORT_ROLE = 50


@pytest_asyncio.fixture
async def service(bot: SimpleNamespace, db: Database) -> TicketService:
    await db.upsert_guild(GuildSettings(guild_id=1, support_roles=[SUPPORT_ROLE]))
    return TicketService(bot)  # type: ignore[arg-type]


async def open_ticket(db: Database, *, owner: int = 5, channel: int = 500) -> Ticket:
    panel = await db.create_panel(
        guild_id=1,
        name="Support",
        support_role_ids=[SUPPORT_ROLE],
        category_id=10,
        transcript_channel_id=20,
        panel_channel_id=30,
    )
    return await db.create_ticket(
        guild_id=1, panel_id=panel.id, channel_id=channel, owner_id=owner, number=1
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Billing Help!", "billing-help"),
        ("  --Ünïcode  name--  ", "n-code-name"),
        ("a" * 150, "a" * 100),
        ("UPPER_case-9", "upper_case-9"),
        ("a   b", "a-b"),
    ],
)
def test_sanitize_channel_name(raw: str, expected: str) -> None:
    assert sanitize_channel_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "\u2603"])
def test_sanitize_rejects_unusable_names(raw: str) -> None:
    with pytest.raises(TicketStateError):
        sanitize_channel_name(raw)


@pytest.mark.asyncio
async def test_limit_blocks_second_ticket_and_links_the_first(
    service: TicketService, db: Database
) -> None:
    await open_ticket(db, owner=5, channel=500)
    existing = make_channel(500)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.return_value = existing
    with pytest.raises(TicketLimitReached) as caught:
        await service._enforce_limit(guild, 5)
    assert caught.value.jump_url == existing.jump_url
    assert existing.jump_url in caught.value.user_message


@pytest.mark.asyncio
async def test_limit_does_not_affect_other_users(service: TicketService, db: Database) -> None:
    await open_ticket(db, owner=5)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.return_value = make_channel(500)
    await service._enforce_limit(guild, 6)  # no raise


@pytest.mark.asyncio
async def test_limit_self_heals_when_channel_was_deleted_manually(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.return_value = None  # channel vanished
    await service._enforce_limit(guild, 5)  # no raise
    healed = await db.get_ticket(ticket.id)
    assert healed is not None
    assert healed.status is TicketStatus.DELETED


@pytest.mark.asyncio
async def test_closed_tickets_do_not_count_towards_limit(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.return_value = make_channel(500)
    await service._enforce_limit(guild, 5)


@pytest.mark.asyncio
async def test_ignore_ticket_id_is_skipped(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db, owner=5)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.return_value = make_channel(500)
    await service._enforce_limit(guild, 5, ignore_ticket_id=ticket.id)


@pytest.mark.asyncio
async def test_claim_release_and_reassign_rules(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    alice = make_member(10, roles=(SUPPORT_ROLE,))
    bob = make_member(11, roles=(SUPPORT_ROLE,))
    admin = make_member(12, manage_guild=True)

    claimed = await service.toggle_claim(channel, ticket, alice, None)
    assert claimed.claimed_by == 10
    assert "Claimed by: user10" in channel.edit.await_args.kwargs["topic"]

    with pytest.raises(TicketStateError, match="Already claimed"):
        await service.toggle_claim(channel, claimed, bob, None)
    with pytest.raises(PermissionDenied):
        await service.toggle_claim(channel, claimed, bob, alice)

    reassigned = await service.toggle_claim(channel, claimed, alice, bob)
    assert reassigned.claimed_by == 11

    # Topic edits share Discord's 2-per-10-minutes budget; let it recover before the 3rd edit.
    service._edit_limiter = ChannelEditLimiter()
    released = await service.toggle_claim(channel, reassigned, admin, bob)
    assert released.claimed_by is None
    assert "Claimed by: nobody" in channel.edit.await_args.kwargs["topic"]
    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.claimed_by is None


@pytest.mark.asyncio
async def test_claim_requires_support_for_invoker_and_target(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    stranger = make_member(20)
    support = make_member(10, roles=(SUPPORT_ROLE,))
    with pytest.raises(PermissionDenied):
        await service.toggle_claim(channel, ticket, stranger, None)
    with pytest.raises(TicketStateError, match="not a support member"):
        await service.toggle_claim(channel, ticket, support, stranger)
    channel.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_refreshes_control_message(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    ticket = await db.modify_ticket(ticket.id, lambda t: replace(t, control_message_id=999))
    channel = make_channel()
    partial = MagicMock()
    partial.edit = AsyncMock()
    channel.get_partial_message.return_value = partial
    await service.toggle_claim(channel, ticket, make_member(10, roles=(SUPPORT_ROLE,)), None)
    channel.get_partial_message.assert_called_once_with(999)
    embed = partial.edit.await_args.kwargs["embed"]
    assert {f.name: f.value for f in embed.fields}["Claimed by"] == "<@10>"


@pytest.mark.asyncio
async def test_cannot_claim_closed_ticket(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    closed = await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    with pytest.raises(TicketStateError):
        await service.toggle_claim(
            make_channel(), closed, make_member(10, roles=(SUPPORT_ROLE,)), None
        )


@pytest.mark.asyncio
async def test_rename_and_revert(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    support = make_member(10, roles=(SUPPORT_ROLE,))
    assert await service.rename_ticket(channel, ticket, support, "Billing Help") == "billing-help"
    assert channel.edit.await_args.kwargs["name"] == "billing-help"
    renamed = await db.get_ticket(ticket.id)
    assert renamed is not None
    assert renamed.custom_name == "billing-help"
    assert await service.rename_ticket(channel, renamed, support, None) == "ticket-0001"


@pytest.mark.asyncio
async def test_rename_requires_support_and_open_state(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    with pytest.raises(PermissionDenied):
        await service.rename_ticket(make_channel(), ticket, make_member(20), "x")
    closed = await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    with pytest.raises(TicketStateError):
        await service.rename_ticket(
            make_channel(), closed, make_member(10, roles=(SUPPORT_ROLE,)), "x"
        )


@pytest.mark.asyncio
async def test_add_user_and_role(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    support = make_member(10, roles=(SUPPORT_ROLE,))
    user, role = make_member(30), make_role(60)
    mentions = await service.add_targets(channel, ticket, support, user, role)
    assert mentions == ["<@30>", "<@&60>"]
    assert channel.set_permissions.await_count == 2
    granted = channel.set_permissions.await_args_list[0].kwargs["overwrite"]
    assert granted.view_channel is True
    assert granted.send_messages is True
    members = await db.list_ticket_members(ticket.id)
    assert {(m.user_id, m.role_id) for m in members} == {(30, None), (None, 60)}


@pytest.mark.asyncio
async def test_add_validation(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    support = make_member(10, roles=(SUPPORT_ROLE,))
    channel = make_channel()
    with pytest.raises(TicketStateError, match="user, a role"):
        await service.add_targets(channel, ticket, support, None, None)
    with pytest.raises(TicketStateError, match="everyone"):
        await service.add_targets(channel, ticket, support, None, make_role(1, default=True))
    with pytest.raises(PermissionDenied):
        await service.add_targets(channel, ticket, make_member(99), make_member(30), None)
    closed = await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    with pytest.raises(TicketStateError, match="open tickets"):
        await service.add_targets(channel, closed, support, make_member(30), None)
    channel.set_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_plain_deletes_overwrite_and_row(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    support = make_member(10, roles=(SUPPORT_ROLE,))
    user = make_member(30)
    await service.add_targets(channel, ticket, support, user, None)
    await service.remove_targets(channel, ticket, support, user, None, revoke=False)
    assert channel.set_permissions.await_args.kwargs["overwrite"] is None
    assert await db.list_ticket_members(ticket.id) == []


@pytest.mark.asyncio
async def test_revoke_blocks_readding_unless_admin(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    support = make_member(10, roles=(SUPPORT_ROLE,))
    admin = make_member(12, manage_guild=True)
    user = make_member(30)
    await service.add_targets(channel, ticket, support, user, None)
    await service.remove_targets(channel, ticket, support, user, None, revoke=True)
    deny = channel.set_permissions.await_args.kwargs["overwrite"]
    assert deny.view_channel is False
    assert await db.is_blocked(ticket.id, user_id=30) is True

    with pytest.raises(TargetBlocked):
        await service.add_targets(channel, ticket, support, user, None)
    await service.add_targets(channel, ticket, admin, user, None)  # admin override
    assert await db.is_blocked(ticket.id, user_id=30) is False


@pytest.mark.asyncio
async def test_owner_and_everyone_cannot_be_removed(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db, owner=5)
    support = make_member(10, roles=(SUPPORT_ROLE,))
    channel = make_channel()
    with pytest.raises(TicketStateError, match="owner"):
        await service.remove_targets(channel, ticket, support, make_member(5), None, revoke=False)
    with pytest.raises(TicketStateError, match="everyone"):
        await service.remove_targets(
            channel, ticket, support, None, make_role(1, default=True), revoke=False
        )
    channel.set_permissions.assert_not_awaited()


def owner_ow() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, send_messages=True, attach_files=True, read_message_history=True
    )


def make_close_env(
    *, owner: MagicMock | None = None, extra: dict | None = None, closed_category: bool = False
) -> tuple[MagicMock, MagicMock, MagicMock]:
    owner = owner or make_member(5)
    transcript_channel = MagicMock(spec=discord.TextChannel)
    transcript_channel.send = AsyncMock(return_value=SimpleNamespace(jump_url="https://t/1"))
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Guild"
    guild.me = make_member(999)
    guild.default_role = make_role(1, default=True)
    closed_cat = MagicMock(spec=discord.CategoryChannel)

    def get_channel(cid: int) -> object | None:
        if cid == 20:
            return transcript_channel
        if cid == 77 and closed_category:
            return closed_cat
        return None

    guild.get_channel.side_effect = get_channel
    channel = make_channel(500, guild)
    channel.overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        owner: owner_ow(),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        **(extra or {}),
    }
    channel.history = MagicMock(return_value=async_iter([make_message("hello", author_id=5)]))
    guild.closed_cat = closed_cat
    return channel, guild, transcript_channel


@pytest.mark.asyncio
async def test_close_locks_first_then_transcripts_then_renames(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    support = make_member(10, roles=(SUPPORT_ROLE,))
    channel, _guild, transcript_channel = make_close_env(owner=owner)
    calls: list[str] = []
    original_edit = channel.edit

    async def edit(**kwargs: object) -> None:
        calls.append("name" if "name" in kwargs else "perms")
        await original_edit(**kwargs)

    channel.edit = edit
    original_history = channel.history

    def history(**kwargs: object) -> object:
        calls.append("history")
        return original_history(**kwargs)

    channel.history = history
    transcript_channel.send.side_effect = lambda **kw: (
        calls.append("upload") or SimpleNamespace(jump_url="https://t/1")
    )

    result = await service.close_ticket(channel, ticket, support, "resolved")

    assert calls == ["perms", "history", "upload", "name"]
    assert result.transcript_url == "https://t/1"
    assert result.renamed is True
    assert result.message_count == 1
    assert result.transcript_path.suffix == ".html"
    body = result.transcript_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in body
    assert "hello" in body
    assert result.transcript_path.parent.name == "1"
    assert transcript_channel.send.await_args is not None


@pytest.mark.asyncio
async def test_close_denies_owner_sending_but_keeps_viewing(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, guild, _ = make_close_env(owner=owner)
    await service.close_ticket(channel, ticket, owner, None)
    locked = channel.edit.await_args_list[0].kwargs["overwrites"][owner]
    assert locked.send_messages is False
    assert locked.attach_files is False
    assert locked.add_reactions is False
    assert locked.send_messages_in_threads is False
    assert locked.create_public_threads is False
    assert locked.create_private_threads is False
    assert locked.view_channel is True
    assert locked.read_message_history is True
    # the bot and @everyone are left alone
    overwrites = channel.edit.await_args_list[0].kwargs["overwrites"]
    assert overwrites[guild.me].send_messages is True
    assert overwrites[guild.default_role].view_channel is False


@pytest.mark.asyncio
async def test_close_also_locks_added_users_added_roles_and_support_roles(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    added_user = make_member(30)
    added_role = make_role(60)
    support_role = make_role(SUPPORT_ROLE)
    channel, _guild, _ = make_close_env(
        extra={added_user: owner_ow(), added_role: owner_ow(), support_role: owner_ow()}
    )
    await service.close_ticket(channel, ticket, make_member(5), None)
    overwrites = channel.edit.await_args_list[0].kwargs["overwrites"]
    for target in (added_user, added_role, support_role):
        assert overwrites[target].send_messages is False, target


@pytest.mark.asyncio
async def test_revoked_users_stay_denied_and_are_not_granted_view(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    revoked = make_member(40)
    channel, _, _ = make_close_env(extra={revoked: discord.PermissionOverwrite(view_channel=False)})
    await service.close_ticket(channel, ticket, make_member(5), None)
    assert channel.edit.await_args_list[0].kwargs["overwrites"][revoked].view_channel is False


@pytest.mark.asyncio
async def test_close_failure_restores_permissions(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, _ = make_close_env(owner=owner)
    original = dict(channel.overwrites)

    def boom(**_: object) -> object:
        raise RuntimeError("history exploded")

    channel.history = boom
    with pytest.raises(RuntimeError):
        await service.close_ticket(channel, ticket, owner, None)
    assert channel.edit.await_args_list[0].kwargs["overwrites"][owner].send_messages is False
    assert channel.edit.await_args.kwargs["overwrites"] == original  # access given back
    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.OPEN


@pytest.mark.asyncio
async def test_close_skips_rename_when_budget_is_spent_but_still_locks(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, guild, _ = make_close_env(owner=owner, closed_category=True)
    await db.upsert_guild(
        GuildSettings(guild_id=1, support_roles=[SUPPORT_ROLE], closed_category=77)
    )
    assert service._edit_limiter.acquire(500)
    assert service._edit_limiter.acquire(500)  # Discord's 2-per-10-minutes budget is gone

    result = await service.close_ticket(channel, ticket, owner, None)

    assert result.renamed is False
    edits = [c.kwargs for c in channel.edit.await_args_list]
    assert not any("name" in e for e in edits)  # never asked Discord to rename
    assert edits[0]["overwrites"][owner].send_messages is False  # still locked
    assert edits[-1]["category"] is guild.closed_cat  # still moved
    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.CLOSED


@pytest.mark.asyncio
async def test_close_does_not_hang_when_discord_stalls_the_rename(
    service: TicketService, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import utils.ticket_service as module

    monkeypatch.setattr(module, "_EDIT_TIMEOUT_SECONDS", 0.05)
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, _ = make_close_env(owner=owner)
    first = channel.edit

    async def edit(**kwargs: object) -> None:
        if "name" in kwargs:
            await asyncio.sleep(5)  # discord.py sleeping through a 429
        await first(**kwargs)

    channel.edit = edit
    result = await service.close_ticket(channel, ticket, owner, None)
    assert result.renamed is False
    assert result.ticket.status is TicketStatus.CLOSED


@pytest.mark.asyncio
async def test_owner_can_close_own_ticket(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, _ = make_close_env(owner=owner)
    result = await service.close_ticket(channel, ticket, owner, None)
    assert result.ticket.status is TicketStatus.CLOSED
    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.transcript_path == str(result.transcript_path)


@pytest.mark.asyncio
async def test_close_survives_transcript_upload_failure(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, transcript_channel = make_close_env(owner=owner)
    transcript_channel.send = AsyncMock(
        side_effect=discord.HTTPException(
            response=SimpleNamespace(status=413, reason="Too large"),
            message="too large",  # type: ignore[arg-type]
        )
    )
    result = await service.close_ticket(channel, ticket, owner, None)
    assert result.transcript_url is None
    assert result.transcript_path.exists()  # still saved locally
    assert result.ticket.status is TicketStatus.CLOSED


@pytest.mark.asyncio
async def test_stranger_cannot_close_and_nothing_is_touched(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    channel, _, _ = make_close_env()
    with pytest.raises(PermissionDenied):
        await service.close_ticket(channel, ticket, make_member(99), None)
    channel.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cannot_close_twice(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db)
    closed = await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    with pytest.raises(TicketStateError, match="already closed"):
        await service.close_ticket(make_channel(), closed, make_member(5), None)


def test_lock_and_unlock_are_inverse_for_participants() -> None:
    owner, support_role, bot_member = make_member(5), make_role(SUPPORT_ROLE), make_member(999)
    everyone = make_role(1, default=True)
    original = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        owner: owner_ow(),
        support_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_messages=True
        ),
        bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    locked = lock_overwrites(original, protect_ids={999, 1})
    assert is_locked(locked[owner])
    assert is_locked(locked[support_role])
    assert locked[support_role].manage_messages is True  # moderation kept while closed
    assert not is_locked(locked[bot_member])
    assert not is_locked(locked[everyone])

    restored = unlock_overwrites(locked, support_role_ids={SUPPORT_ROLE}, manage_messages=True)
    assert restored[owner].send_messages is True
    assert restored[owner].add_reactions is None  # extra denies removed
    assert restored[support_role].manage_messages is True
    assert restored[bot_member] is original[bot_member]


@pytest.mark.asyncio
async def test_reopen_gives_write_access_back_and_renames(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    closed = await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    owner = make_member(5)
    channel, _guild, _ = make_close_env(owner=owner)
    channel.overwrites = lock_overwrites(channel.overwrites, protect_ids={999, 1})
    assert is_locked(channel.overwrites[owner])

    result = await service.reopen_ticket(channel, closed, make_member(10, roles=(SUPPORT_ROLE,)))

    assert result.ticket.status is TicketStatus.OPEN
    assert result.renamed is True
    first = channel.edit.await_args_list[0].kwargs
    assert first["overwrites"][owner].send_messages is True
    assert channel.edit.await_args_list[1].kwargs["name"] == "ticket-0001"


@pytest.mark.asyncio
async def test_reopen_without_rename_budget_still_unlocks(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    closed = await db.modify_ticket(ticket.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    owner = make_member(5)
    channel, _, _ = make_close_env(owner=owner)
    channel.overwrites = lock_overwrites(channel.overwrites, protect_ids={999, 1})
    service._edit_limiter.acquire(500)
    service._edit_limiter.acquire(500)
    result = await service.reopen_ticket(channel, closed, make_member(10, roles=(SUPPORT_ROLE,)))
    assert result.renamed is False
    assert channel.edit.await_args.kwargs["overwrites"][owner].send_messages is True
    assert not any("name" in c.kwargs for c in channel.edit.await_args_list)


@pytest.mark.asyncio
async def test_reopen_checks_state_permission_and_limit(
    service: TicketService, db: Database
) -> None:
    support = make_member(10, roles=(SUPPORT_ROLE,))
    still_open = await open_ticket(db, owner=5, channel=500)
    with pytest.raises(TicketStateError, match="not closed"):
        await service.reopen_ticket(make_channel(), still_open, support)

    closed = await db.modify_ticket(still_open.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    with pytest.raises(PermissionDenied):
        await service.reopen_ticket(make_channel(), closed, make_member(99))

    # The owner opened a second ticket meanwhile, so reopening must respect the limit.
    await db.create_ticket(guild_id=1, panel_id=1, channel_id=501, owner_id=5, number=2)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.side_effect = lambda cid: make_channel(cid) if cid == 501 else None
    channel = make_channel(500, guild)
    with pytest.raises(TicketLimitReached):
        await service.reopen_ticket(channel, closed, support)
    channel.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_refused_with_wait_time_and_db_untouched_when_budget_spent(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    service._edit_limiter.acquire(500)
    service._edit_limiter.acquire(500)
    with pytest.raises(TicketStateError, match=r"2 channel renames per 10 minutes.*Try again in"):
        await service.rename_ticket(
            channel, ticket, make_member(10, roles=(SUPPORT_ROLE,)), "billing"
        )
    channel.edit.assert_not_awaited()
    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.custom_name is None


@pytest.mark.asyncio
async def test_third_rename_in_a_row_is_refused_locally(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    support = make_member(10, roles=(SUPPORT_ROLE,))
    await service.rename_ticket(channel, ticket, support, "one")
    await service.rename_ticket(channel, ticket, support, "two")
    with pytest.raises(TicketStateError):
        await service.rename_ticket(channel, ticket, support, "three")
    assert channel.edit.await_count == 2  # Discord was never asked a third time


@pytest.mark.asyncio
async def test_claim_still_works_when_topic_budget_is_spent(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    service._edit_limiter.acquire(500)
    service._edit_limiter.acquire(500)
    claimed = await service.toggle_claim(
        channel, ticket, make_member(10, roles=(SUPPORT_ROLE,)), None
    )
    assert claimed.claimed_by == 10
    channel.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_removes_channel_and_marks_deleted(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    channel.delete = AsyncMock()
    await service.delete_ticket(channel, ticket, make_member(10, roles=(SUPPORT_ROLE,)))
    channel.delete.assert_awaited_once()
    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.DELETED
    assert stored.closed_at is not None


@pytest.mark.asyncio
async def test_delete_denied_for_non_support(service: TicketService, db: Database) -> None:
    ticket = await open_ticket(db, owner=5)
    channel = make_channel()
    channel.delete = AsyncMock()
    with pytest.raises(PermissionDenied):
        await service.delete_ticket(channel, ticket, make_member(5))  # even the owner
    channel.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_channel_delete_leaves_ticket_open(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    channel.delete = AsyncMock(side_effect=RuntimeError("discord down"))
    with pytest.raises(RuntimeError):
        await service.delete_ticket(channel, ticket, make_member(10, roles=(SUPPORT_ROLE,)))
    stored = await db.get_ticket(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.OPEN


@pytest.mark.asyncio
async def _close_inter(
    service: TicketService, db: Database, *, upload_ok: bool = True, budget_spent: bool = False
) -> tuple[MagicMock, Ticket]:
    from tests.fakes import make_interaction

    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, transcript_channel = make_close_env(owner=owner)
    if not upload_ok:
        transcript_channel.send = AsyncMock(
            side_effect=discord.HTTPException(
                response=SimpleNamespace(status=413, reason="x"),
                message="x",  # type: ignore[arg-type]
            )
        )
    if budget_spent:
        # noinspection PyProtectedMember
        service._edit_limiter.acquire(500)
        # noinspection PyProtectedMember
        service._edit_limiter.acquire(500)
    inter = make_interaction(user=owner, guild=channel.guild, channel_id=500)
    inter.channel = channel
    return inter, ticket


@pytest.mark.asyncio
async def test_close_reply_links_transcript_and_offers_reopen_delete(
    service: TicketService, db: Database
) -> None:
    inter, ticket = await _close_inter(service, db)
    await service.close_via_interaction(inter, ticket, "all done")
    sent = inter.followup.send.await_args.kwargs
    fields = {f.name: f.value for f in sent["embed"].fields}
    assert "https://t/1" in fields["Transcript"]
    assert fields["Reason"] == "all done"
    assert sent["embed"].footer.text is None
    assert {c.label for c in sent["view"].children} == {"Reopen", "Delete"}
    assert "file" not in sent


@pytest.mark.asyncio
async def test_close_reply_attaches_transcript_when_upload_failed(
    service: TicketService, db: Database
) -> None:
    inter, ticket = await _close_inter(service, db, upload_ok=False)
    await service.close_via_interaction(inter, ticket, None)
    sent = inter.followup.send.await_args.kwargs
    assert sent["file"].filename.endswith(".html")


@pytest.mark.asyncio
async def test_close_reply_mentions_skipped_rename(service: TicketService, db: Database) -> None:
    inter, ticket = await _close_inter(service, db, budget_spent=True)
    await service.close_via_interaction(inter, ticket, None)
    assert "rate limit" in inter.followup.send.await_args.kwargs["embed"].footer.text


@pytest.mark.asyncio
async def test_close_reply_rejects_non_text_channels(service: TicketService, db: Database) -> None:
    from tests.fakes import make_interaction

    ticket = await open_ticket(db)
    inter = make_interaction(user=make_member(5), channel_id=1)
    inter.channel = MagicMock(spec=discord.Thread)
    with pytest.raises(TicketStateError):
        await service.close_via_interaction(inter, ticket, None)


def http_error(exc: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return exc(
        response=SimpleNamespace(status=status, reason="x"),  # type: ignore[arg-type]
        message="x",
    )


async def archive_env(
    service: TicketService,
    db: Database,
    tmp_path: Path,
    *,
    archived: int,
    with_transcripts: bool = True,
    track_tickets: bool = True,
) -> tuple[MagicMock, MagicMock, Ticket, Database, list[MagicMock]]:
    owner = make_member(5)
    ticket = await open_ticket(db, owner=5)
    channel, guild, _ = make_close_env(owner=owner, closed_category=True)
    await db.upsert_guild(
        GuildSettings(guild_id=1, support_roles=[SUPPORT_ROLE], closed_category=77)
    )
    panel_id = ticket.panel_id
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    victims: list[MagicMock] = []
    for i in range(archived):
        victim = make_channel(2000 + i, guild)
        victim.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        victim.delete = AsyncMock()
        victim.history = MagicMock(return_value=async_iter([make_message("old")]))
        victims.append(victim)
        if not track_tickets:
            continue
        row = await db.create_ticket(
            guild_id=1, panel_id=panel_id, channel_id=victim.id, owner_id=6, number=i + 2
        )
        transcript = tmp_path / f"old-{i}.html"
        if with_transcripts:
            transcript.write_text("<html></html>", encoding="utf-8")
        await db.modify_ticket(
            row.id,
            lambda t, i=i, transcript=transcript, base=base: replace(
                t,
                status=TicketStatus.CLOSED,
                closed_at=(base + timedelta(hours=i)).isoformat(),
                transcript_path=str(transcript),
            ),
        )
    guild.closed_cat.id = 77
    guild.closed_cat.guild = guild
    guild.closed_cat.text_channels = list(reversed(victims))
    return channel, guild, ticket, db, victims


async def status_of(db: Database, channel_id: int) -> TicketStatus:
    row = next(t for t in await db.list_tickets(1) if t.channel_id == channel_id)
    return row.status


@pytest.mark.asyncio
async def test_double_close_runs_once(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, _ = make_close_env(owner=owner)
    real = service.write_transcript
    calls: list[int] = []

    async def slow(ch: MagicMock, t: Ticket) -> tuple[Path, int]:
        calls.append(t.id)
        await asyncio.sleep(0.05)  # hold the first close open so the second overlaps it
        return await real(ch, t)

    service.write_transcript = slow  # type: ignore[method-assign]
    results = await asyncio.gather(
        service.close_ticket(channel, ticket, owner, None),
        service.close_ticket(channel, ticket, owner, None),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    refused = [r for r in results if isinstance(r, TicketStateError)]
    assert len(refused) == 1
    assert "already being closed" in refused[0].user_message
    assert calls == [ticket.id]  # one transcript, not two
    assert service._closing == set()


@pytest.mark.asyncio
async def test_close_lock_is_released_after_a_failure(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, _ = make_close_env(owner=owner)
    real = service.write_transcript
    service.write_transcript = AsyncMock(side_effect=RuntimeError("disk full"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await service.close_ticket(channel, ticket, owner, None)
    assert service._closing == set()
    service.write_transcript = real  # type: ignore[method-assign]
    channel.history = MagicMock(return_value=async_iter([make_message("hello")]))
    result = await service.close_ticket(channel, ticket, owner, None)  # retry succeeds
    assert result.ticket.status is TicketStatus.CLOSED


@pytest.mark.asyncio
async def test_stale_ticket_object_cannot_start_a_second_close(
    service: TicketService, db: Database
) -> None:
    stale = await open_ticket(db, owner=5)
    await db.modify_ticket(stale.id, lambda t: replace(t, status=TicketStatus.CLOSED))
    channel, _, _ = make_close_env()
    with pytest.raises(TicketStateError, match="already closed"):
        await service.close_ticket(channel, stale, make_member(5), None)  # stale says OPEN
    channel.edit.assert_not_awaited()
    assert service._closing == set()


@pytest.mark.asyncio
async def test_close_of_a_vanished_channel_is_graceful(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db, owner=5)
    owner = make_member(5)
    channel, _, _ = make_close_env(owner=owner)
    channel.edit = AsyncMock(side_effect=http_error(discord.NotFound, 404))
    with pytest.raises(TicketStateError, match="no longer exists"):
        await service.close_ticket(channel, ticket, owner, None)
    assert (await db.get_ticket(ticket.id)).status is TicketStatus.DELETED  # type: ignore[union-attr]
    assert service._closing == set()


@pytest.mark.asyncio
async def test_no_prune_below_the_safe_limit(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, guild, ticket, _, victims = await archive_env(service, db, tmp_path, archived=44)
    result = await service.close_ticket(channel, ticket, make_member(5), None)
    assert result.ticket.status is TicketStatus.CLOSED
    assert all(v.delete.await_count == 0 for v in victims)
    assert channel.edit.await_args_list[-1].kwargs["category"] is guild.closed_cat


@pytest.mark.asyncio
async def test_prune_at_the_limit_deletes_only_the_oldest_then_moves_in(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, guild, ticket, _, victims = await archive_env(service, db, tmp_path, archived=45)
    result = await service.close_ticket(channel, ticket, make_member(5), None)
    assert result.ticket.status is TicketStatus.CLOSED
    victims[0].delete.assert_awaited_once()
    assert all(v.delete.await_count == 0 for v in victims[1:])
    assert await status_of(db, victims[0].id) is TicketStatus.DELETED
    assert await status_of(db, victims[1].id) is TicketStatus.CLOSED
    assert channel.edit.await_args_list[-1].kwargs["category"] is guild.closed_cat


@pytest.mark.asyncio
async def test_prune_regenerates_a_missing_transcript_before_deleting(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, _, ticket, _, victims = await archive_env(
        service, db, tmp_path, archived=45, with_transcripts=False
    )
    order: list[str] = []
    real = service.write_transcript

    async def spy(ch: MagicMock, t: Ticket) -> tuple[Path, int]:
        order.append(f"transcript:{ch.id}")
        return await real(ch, t)

    service.write_transcript = spy  # type: ignore[method-assign]
    victims[0].delete = AsyncMock(side_effect=lambda **_: order.append("delete:2000"))
    await service.close_ticket(channel, ticket, make_member(5), None)
    assert order.index("transcript:2000") < order.index("delete:2000")
    row = next(t for t in await db.list_tickets(1) if t.channel_id == 2000)
    assert row.transcript_path is not None and Path(row.transcript_path).is_file()


@pytest.mark.asyncio
async def test_prune_skips_a_victim_whose_transcript_cannot_be_saved(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, _, ticket, _, victims = await archive_env(
        service, db, tmp_path, archived=45, with_transcripts=False
    )
    victims[0].history = MagicMock(side_effect=RuntimeError("cannot read history"))
    result = await service.close_ticket(channel, ticket, make_member(5), None)
    assert result.ticket.status is TicketStatus.CLOSED
    victims[0].delete.assert_not_awaited()  # never deleted without a transcript
    assert await status_of(db, victims[0].id) is TicketStatus.CLOSED
    victims[1].delete.assert_awaited_once()  # next oldest made the room instead


@pytest.mark.asyncio
async def test_failed_prune_never_blocks_the_close(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, guild, ticket, _, victims = await archive_env(service, db, tmp_path, archived=45)
    for v in victims:
        v.delete = AsyncMock(side_effect=http_error(discord.Forbidden, 403))
    result = await service.close_ticket(channel, ticket, make_member(5), None)
    assert result.ticket.status is TicketStatus.CLOSED
    assert channel.edit.await_args_list[-1].kwargs["category"] is guild.closed_cat
    assert await status_of(db, victims[0].id) is TicketStatus.CLOSED  # DB not marked deleted
    assert sum(v.delete.await_count for v in victims) == 3  # bounded retries


@pytest.mark.asyncio
async def test_full_archive_move_failure_still_finishes_the_close(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, _, ticket, _, victims = await archive_env(service, db, tmp_path, archived=45)
    for v in victims:
        v.delete = AsyncMock(side_effect=http_error(discord.Forbidden, 403))
    channel.edit = AsyncMock(
        side_effect=[None, http_error(discord.HTTPException, 400), http_error(discord.HTTPException, 400)]
    )
    result = await service.close_ticket(channel, ticket, make_member(5), None)
    assert result.ticket.status is TicketStatus.CLOSED  # DB consistent despite the failed move


@pytest.mark.asyncio
async def test_unrelated_channels_in_the_archive_are_never_pruned(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, _, ticket, _, strangers = await archive_env(
        service, db, tmp_path, archived=45, track_tickets=False
    )
    result = await service.close_ticket(channel, ticket, make_member(5), None)
    assert result.ticket.status is TicketStatus.CLOSED
    assert all(c.delete.await_count == 0 for c in strangers)


@pytest.mark.asyncio
async def test_double_close_on_a_full_archive_prunes_exactly_once(
    service: TicketService, db: Database, tmp_path: Path
) -> None:
    channel, _, ticket, _, victims = await archive_env(service, db, tmp_path, archived=45)
    owner = make_member(5)
    real = service.write_transcript

    async def slow(ch: MagicMock, t: Ticket) -> tuple[Path, int]:
        await asyncio.sleep(0.05)
        return await real(ch, t)

    service.write_transcript = slow  # type: ignore[method-assign]
    await asyncio.gather(
        service.close_ticket(channel, ticket, owner, None),
        service.close_ticket(channel, ticket, owner, None),
        return_exceptions=True,
    )
    assert sum(v.delete.await_count for v in victims) == 1


@pytest.mark.asyncio
async def test_delete_of_an_already_deleted_channel_still_marks_the_row(
    service: TicketService, db: Database
) -> None:
    ticket = await open_ticket(db)
    channel = make_channel()
    channel.delete = AsyncMock(side_effect=http_error(discord.NotFound, 404))
    await service.delete_ticket(channel, ticket, make_member(10, roles=(SUPPORT_ROLE,)))
    assert (await db.get_ticket(ticket.id)).status is TicketStatus.DELETED  # type: ignore[union-attr]
