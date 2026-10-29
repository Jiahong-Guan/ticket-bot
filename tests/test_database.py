from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from utils.database import (
    AtomicJSONStorage,
    Database,
    GuildSettings,
    TicketStatus,
)
from utils.exceptions import DatabaseError

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


async def make_panel(db: Database, guild_id: int = 1) -> int:
    panel = await db.create_panel(
        guild_id=guild_id,
        name="Support",
        support_role_ids=[10, 11],
        category_id=20,
        transcript_channel_id=30,
        panel_channel_id=40,
    )
    return panel.id


@pytest.mark.asyncio
async def test_unknown_guild_returns_defaults(db: Database) -> None:
    g = await db.get_guild(99)
    assert g == GuildSettings(guild_id=99)


@pytest.mark.asyncio
async def test_ticket_numbers_are_sequential_per_guild(db: Database) -> None:
    assert [await db.next_ticket_number(1) for _ in range(3)] == [1, 2, 3]
    assert await db.next_ticket_number(2) == 1


@pytest.mark.asyncio
async def test_ticket_numbers_are_unique_under_concurrency(db: Database) -> None:
    numbers = await asyncio.gather(*(db.next_ticket_number(1) for _ in range(40)))
    assert sorted(numbers) == list(range(1, 41))


@pytest.mark.asyncio
async def test_upsert_never_overwrites_the_counter(db: Database) -> None:
    await db.next_ticket_number(1)
    await db.next_ticket_number(1)
    stale = GuildSettings(guild_id=1, support_roles=[5], ticket_counter=0)
    stored = await db.upsert_guild(stale)
    assert stored.support_roles == [5]
    assert stored.ticket_counter == 2
    assert await db.next_ticket_number(1) == 3


@pytest.mark.asyncio
async def test_modify_guild_is_atomic_read_modify_write(db: Database) -> None:
    def add(role: int):  # type: ignore[no-untyped-def]
        return lambda g: replace(g, support_roles=[*g.support_roles, role])

    await asyncio.gather(*(db.modify_guild(1, add(i)) for i in range(20)))
    assert sorted((await db.get_guild(1)).support_roles) == list(range(20))


@pytest.mark.asyncio
async def test_modify_guild_keeps_counter(db: Database) -> None:
    await db.next_ticket_number(1)
    updated = await db.modify_guild(1, lambda g: replace(g, ticket_counter=999, panel_channel=3))
    assert updated.panel_channel == 3
    assert (await db.get_guild(1)).ticket_counter == 1


@pytest.mark.asyncio
async def test_panel_lifecycle(db: Database) -> None:
    panel_id = await make_panel(db)
    panel = await db.get_panel(panel_id)
    assert panel is not None
    assert panel.id == panel_id
    assert panel.support_role_ids == [10, 11]
    assert panel.message_id is None

    await db.update_panel(replace(panel, message_id=77))
    reloaded = await db.get_panel(panel_id)
    assert reloaded is not None
    assert reloaded.message_id == 77

    assert await db.delete_panel(panel_id) is True
    assert await db.delete_panel(panel_id) is False
    assert await db.get_panel(panel_id) is None


@pytest.mark.asyncio
async def test_list_panels_filters_by_guild(db: Database) -> None:
    await make_panel(db, 1)
    await make_panel(db, 1)
    await make_panel(db, 2)
    assert len(await db.list_panels()) == 3
    assert len(await db.list_panels(1)) == 2


@pytest.mark.asyncio
async def test_panel_ids_are_unique(db: Database) -> None:
    ids = {await make_panel(db) for _ in range(5)}
    assert len(ids) == 5


@pytest.mark.asyncio
async def new_ticket(db: Database, *, owner: int = 5, channel: int = 100, guild: int = 1):  # type: ignore[no-untyped-def]
    number = await db.peek_ticket_number(guild)
    return await db.create_ticket(
        guild_id=guild, panel_id=1, channel_id=channel, owner_id=owner, number=number
    )


@pytest.mark.asyncio
async def test_ticket_roundtrip_and_defaults(db: Database) -> None:
    t = await new_ticket(db)
    assert t.status is TicketStatus.OPEN
    assert t.claimed_by is None
    fetched = await db.get_ticket(t.id)
    assert fetched == t
    assert (await db.get_ticket_by_channel(100)) == t


@pytest.mark.asyncio
async def test_deleted_tickets_are_not_found_by_channel(db: Database) -> None:
    t = await new_ticket(db)
    await db.modify_ticket(t.id, lambda x: replace(x, status=TicketStatus.DELETED))
    assert await db.get_ticket_by_channel(100) is None
    assert (await db.get_ticket(t.id)) is not None


@pytest.mark.asyncio
async def test_list_tickets_filters(db: Database) -> None:
    a = await new_ticket(db, owner=1, channel=1)
    await new_ticket(db, owner=2, channel=2)
    await new_ticket(db, owner=1, channel=3, guild=9)
    await db.modify_ticket(a.id, lambda x: replace(x, status=TicketStatus.CLOSED))
    assert len(await db.list_tickets()) == 3
    assert len(await db.list_tickets(1)) == 2
    assert len(await db.list_tickets(owner_id=1)) == 2
    assert [t.id for t in await db.list_tickets(status=TicketStatus.CLOSED)] == [a.id]
    assert len(await db.list_tickets(1, owner_id=1, status=TicketStatus.OPEN)) == 0


@pytest.mark.asyncio
async def test_modify_missing_ticket_raises(db: Database) -> None:
    with pytest.raises(DatabaseError):
        await db.modify_ticket(12345, lambda t: t)


@pytest.mark.asyncio
async def test_peek_returns_one_for_new_guild(db: Database) -> None:
    assert await db.peek_ticket_number(1) == 1


@pytest.mark.asyncio
async def test_peek_returns_counter_plus_one(db: Database) -> None:
    await new_ticket(db)  # counter → 1
    assert await db.peek_ticket_number(1) == 2
    await new_ticket(db)  # counter → 2
    assert await db.peek_ticket_number(1) == 3


@pytest.mark.asyncio
async def test_peek_is_idempotent(db: Database) -> None:
    # Calling peek N times must return the same value every time
    assert [await db.peek_ticket_number(1) for _ in range(5)] == [1] * 5


@pytest.mark.asyncio
async def test_peek_does_not_advance_the_counter(db: Database) -> None:
    # peek shouldn't write to storage at all
    await new_ticket(db)  # counter → 1
    before = await db.get_guild(1)
    await db.peek_ticket_number(1)
    after = await db.get_guild(1)
    assert before == after


@pytest.mark.asyncio
async def test_peek_does_not_create_a_guild_row(db: Database) -> None:
    await db.peek_ticket_number(42)
    data = json.loads((db.path / "guilds.json").read_text(encoding="utf-8"))
    rows = data.get("_default", {}).values()
    assert all(row.get("guild_id") != 42 for row in rows)


@pytest.mark.asyncio
async def test_peek_is_independent_per_guild(db: Database) -> None:
    await new_ticket(db, guild=10)
    await new_ticket(db, guild=10)
    await new_ticket(db, guild=20)
    assert await db.peek_ticket_number(10) == 3
    assert await db.peek_ticket_number(20) == 2
    assert await db.peek_ticket_number(30) == 1


@pytest.mark.asyncio
async def test_peek_is_consistent_under_concurrency(db: Database) -> None:
    # 20 concurrent peeks must all see the same value (no double-bumps, no races)
    await new_ticket(db)  # counter → 1
    numbers = await asyncio.gather(*(db.peek_ticket_number(1) for _ in range(20)))
    assert numbers == [2] * 20


@pytest.mark.asyncio
async def test_peek_matches_create_ticket_expectation(db: Database) -> None:
    # The value peek returns must be accepted by create_ticket(number=...)
    for _ in range(3):
        number = await db.peek_ticket_number(1)
        ticket = await db.create_ticket(
            guild_id=1, panel_id=1, channel_id=number, owner_id=1, number=number
        )
        assert ticket.number == number
    assert await db.peek_ticket_number(1) == 4


@pytest.mark.asyncio
async def test_peek_after_deleted_ticket_still_advances(db: Database) -> None:
    # Deleting tickets doesn't rewind the counter — numbers are never reused
    t = await new_ticket(db)
    await db.modify_ticket(t.id, lambda x: replace(x, status=TicketStatus.DELETED))
    assert await db.peek_ticket_number(1) == 2


@pytest.mark.asyncio
async def test_peek_does_not_touch_other_guild_fields(db: Database) -> None:
    await db.upsert_guild(GuildSettings(guild_id=1, support_roles=[7, 8]))
    before = await db.get_guild(1)
    await db.peek_ticket_number(1)
    after = await db.get_guild(1)
    assert before == after
    assert after.support_roles == [7, 8]


@pytest.mark.asyncio
async def test_concurrent_modifications_do_not_lose_updates(db: Database) -> None:
    t = await new_ticket(db)
    await asyncio.gather(
        db.modify_ticket(t.id, lambda x: replace(x, claimed_by=9)),
        db.modify_ticket(t.id, lambda x: replace(x, close_reason="why")),
    )
    final = await db.get_ticket(t.id)
    assert final is not None
    assert final.claimed_by == 9
    assert final.close_reason == "why"


@pytest.mark.asyncio
async def test_member_upsert_list_remove(db: Database) -> None:
    await db.set_ticket_member(1, user_id=5)
    await db.set_ticket_member(1, user_id=5)  # idempotent
    await db.set_ticket_member(1, role_id=8)
    members = await db.list_ticket_members(1)
    assert len(members) == 2
    assert await db.remove_ticket_member(1, user_id=5) is True
    assert await db.remove_ticket_member(1, user_id=5) is False
    assert [m.role_id for m in await db.list_ticket_members(1)] == [8]


@pytest.mark.asyncio
async def test_blocklist(db: Database) -> None:
    assert await db.is_blocked(1, user_id=5) is False
    await db.set_ticket_member(1, user_id=5, blocked=True)
    assert await db.is_blocked(1, user_id=5) is True
    assert await db.is_blocked(1, user_id=6) is False
    assert await db.is_blocked(2, user_id=5) is False
    await db.set_ticket_member(1, user_id=5, blocked=False)
    assert await db.is_blocked(1, user_id=5) is False


@pytest.mark.asyncio
async def test_member_requires_exactly_one_target(db: Database) -> None:
    with pytest.raises(DatabaseError):
        await db.set_ticket_member(1)
    with pytest.raises(DatabaseError):
        await db.set_ticket_member(1, user_id=1, role_id=2)
    with pytest.raises(DatabaseError):
        await db.remove_ticket_member(1)
    with pytest.raises(DatabaseError):
        await db.is_blocked(1)


@pytest.mark.asyncio
async def test_data_survives_reopen(tmp_path: Path) -> None:
    first = Database(tmp_path / "db")
    await first.open()
    await first.next_ticket_number(1)
    await first.close()

    second = Database(tmp_path / "db")
    await second.open()
    assert await second.next_ticket_number(1) == 2
    await second.close()


@pytest.mark.asyncio
async def test_one_json_file_per_table(db: Database) -> None:
    names = sorted(p.name for p in db.path.glob("*.json"))
    assert names == ["guilds.json", "panels.json", "ticket_members.json", "tickets.json"]


@pytest.mark.asyncio
async def test_no_tmp_files_left_after_writes(db: Database) -> None:
    await db.next_ticket_number(1)
    assert list(db.path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_failed_replace_keeps_old_data(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    await db.next_ticket_number(1)

    def boom(*_: object, **__: object) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(DatabaseError):
        await db.next_ticket_number(1)
    monkeypatch.undo()
    # The real file still holds the last good state: counter 1.
    data = json.loads((db.path / "guilds.json").read_text(encoding="utf-8"))
    assert next(iter(data["_default"].values()))["ticket_counter"] == 1


@pytest.mark.asyncio
async def test_stale_tmp_file_does_not_corrupt_next_write(db: Database) -> None:
    (db.path / "guilds.json.tmp").write_text("{garbage", encoding="utf-8")
    assert await db.next_ticket_number(1) == 1
    json.loads((db.path / "guilds.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_corrupt_table_raises_database_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "db")
    await db.open()
    (db.path / "guilds.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(DatabaseError, match="corrupt"):
        await db.get_guild(1)
    await db.close()


@pytest.mark.asyncio
async def test_atomic_write_rewrites_table(db: Database) -> None:
    await db.next_ticket_number(1)
    await db.atomic_write("guilds")
    assert (await db.get_guild(1)).ticket_counter == 1
    with pytest.raises(DatabaseError):
        await db.atomic_write("nope")


def test_storage_reads_empty_file_as_none(tmp_path: Path) -> None:
    storage = AtomicJSONStorage(tmp_path / "x.json")
    (tmp_path / "x.json").write_text("", encoding="utf-8")
    assert storage.read() is None
