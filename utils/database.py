from __future__ import annotations

import asyncio
import contextlib
import json
import os
import warnings
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from tinydb import Query, TinyDB
from tinydb.storages import Storage

from utils.exceptions import DatabaseError
from utils.logger import get_logger

log = get_logger("database")

T = TypeVar("T")
M = TypeVar("M", bound="_Model")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TicketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    DELETED = "deleted"


class _Model:
    __slots__ = ()
    _enum_fields: ClassVar[dict[str, type[Enum]]] = {}

    def to_doc(self) -> dict[str, Any]:
        data = asdict(self)  # type: ignore[call-overload]
        return {k: (v.value if isinstance(v, Enum) else v) for k, v in data.items()}

    @classmethod
    def from_doc(cls: type[M], doc: Mapping[str, Any]) -> M:
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        data = {k: v for k, v in doc.items() if k in names}
        for name, enum_cls in cls._enum_fields.items():
            if name in data:
                data[name] = enum_cls(data[name])
        return cls(**data)


@dataclass(slots=True)
class GuildSettings(_Model):
    guild_id: int
    support_roles: list[int] = field(default_factory=list)
    ticket_category: int | None = None
    closed_category: int | None = None
    transcript_channel: int | None = None
    panel_channel: int | None = None
    panel_message_id: int | None = None
    ticket_counter: int = 0


@dataclass(slots=True)
class Panel(_Model):
    id: int
    guild_id: int
    name: str
    support_role_ids: list[int]
    category_id: int
    transcript_channel_id: int
    panel_channel_id: int
    message_id: int | None = None
    button_label: str = "Create Ticket"


@dataclass(slots=True)
class Ticket(_Model):
    _enum_fields: ClassVar[dict[str, type[Enum]]] = {"status": TicketStatus}

    id: int
    guild_id: int
    panel_id: int
    channel_id: int
    owner_id: int
    number: int
    status: TicketStatus = TicketStatus.OPEN
    claimed_by: int | None = None
    created_at: str = field(default_factory=utcnow_iso)
    closed_at: str | None = None
    transcript_path: str | None = None
    control_message_id: int | None = None
    close_reason: str | None = None
    custom_name: str | None = None


@dataclass(slots=True)
class TicketMember(_Model):
    ticket_id: int
    user_id: int | None = None
    role_id: int | None = None
    added_at: str = field(default_factory=utcnow_iso)
    blocked: bool = False


class AtomicJSONStorage(Storage):
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.write({})

    def read(self) -> dict[str, dict[str, Any]] | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if not text.strip():
            return None
        try:
            data: dict[str, dict[str, Any]] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DatabaseError(f"{self.path.name} is corrupt: {exc}") from exc
        return data

    def write(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ": "),
                indent=2
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self._fsync_directory()

    def _fsync_directory(self) -> None:
        if os.name != "posix":
            return

        with contextlib.suppress(OSError):
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def close(self) -> None:
        """Nothing to release; the file is opened per operation."""


class Database:
    TABLES: ClassVar[tuple[str, ...]] = ("guilds", "panels", "tickets", "ticket_members")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._tables: dict[str, TinyDB] = {}

    async def open(self) -> None:
        def _open() -> None:
            self.path.mkdir(parents=True, exist_ok=True)
            for name in self.TABLES:
                self._tables[name] = TinyDB(self.path / f"{name}.json", storage=AtomicJSONStorage)

        await self._run(_open)
        log.info(f"Database opened at {self.path} ({', '.join(self.TABLES)})")

    async def close(self) -> None:
        def _close() -> None:
            for db in self._tables.values():
                db.close()
            self._tables.clear()

        await self._run(_close)

    async def atomic_write(self, table: str) -> None:
        if table not in self.TABLES:
            raise DatabaseError(f"Unknown table '{table}'")

        def _rewrite() -> None:
            storage = self._tables[table]._storage
            data = storage.read() or {}
            storage.write(data)

        await self._run(_rewrite)

    async def _run(self, func: Callable[[], T]) -> T:
        async with self._lock:
            try:
                return await asyncio.to_thread(func)
            except DatabaseError:
                log.exception("Database operation failed")
                raise
            except Exception as exc:
                log.exception("Unexpected database failure")
                raise DatabaseError(str(exc)) from exc

    def _first(self, table: str, cond: Any) -> Mapping[str, Any] | None:
        rows = self._tables[table].search(cond)
        return rows[0] if rows else None

    async def get_guild(self, guild_id: int) -> GuildSettings:
        def op() -> GuildSettings:
            doc = self._first("guilds", Query().guild_id == guild_id)
            return GuildSettings.from_doc(doc) if doc else GuildSettings(guild_id=guild_id)

        return await self._run(op)

    async def upsert_guild(self, settings):
        def op() -> GuildSettings:
            table = self._tables["guilds"]
            cond = Query().guild_id == settings.guild_id
            doc = settings.to_doc()
            doc.pop("ticket_counter")
            if self._first("guilds", cond):
                table.update(doc, cond)
            else:
                table.insert({**doc, "ticket_counter": 0})
            stored = self._first("guilds", cond)
            return GuildSettings.from_doc(stored or doc)

        return await self._run(op)

    async def modify_guild(
            self, guild_id: int, mutate: Callable[[GuildSettings], GuildSettings]
    ) -> GuildSettings:
        def op() -> GuildSettings:
            table = self._tables["guilds"]
            cond = Query().guild_id == guild_id
            doc = self._first("guilds", cond)
            current = GuildSettings.from_doc(doc) if doc else GuildSettings(guild_id=guild_id)
            updated = mutate(current)
            data = updated.to_doc()
            data["ticket_counter"] = current.ticket_counter
            if doc:
                table.update(data, cond)
            else:
                table.insert(data)
            return replace(updated, ticket_counter=current.ticket_counter)

        return await self._run(op)

    async def next_ticket_number(self, guild_id: int) -> int:
        """
        Temporarily Deprecated Method. Use peek_ticket_number() + create_ticket(number=...) instead

        WARNING: consumes a ticket number immediately. If the caller then fails
        to create the ticket, the number is permanently burned. Do NOT use this
        in the ticket-creation path — use peek_ticket_number + create_ticket(number=...)
        so the bump is atomic with the insert.
        """
        warnings.warn(
            "Use peek_ticket_number() + create_ticket(number=...) instead",
            DeprecationWarning,
            stacklevel=2,
        )

        def op() -> int:
            table = self._tables["guilds"]
            cond = Query().guild_id == guild_id
            doc = self._first("guilds", cond)
            if doc is None:
                table.insert(GuildSettings(guild_id=guild_id, ticket_counter=1).to_doc())
                return 1
            number = int(doc.get("ticket_counter", 0)) + 1
            table.update({"ticket_counter": number}, cond)
            return number

        return await self._run(op)

    async def peek_ticket_number(self, guild_id: int) -> int:
        def op() -> int:
            doc = self._first("guilds", Query().guild_id == guild_id)
            return int(doc.get("ticket_counter", 0)) + 1 if doc else 1

        return await self._run(op)

    async def create_panel(
            self,
            *,
            guild_id: int,
            name: str,
            support_role_ids: list[int],
            category_id: int,
            transcript_channel_id: int,
            panel_channel_id: int,
            button_label: str = "Create Ticket",
    ) -> Panel:
        def op() -> Panel:
            table = self._tables["panels"]
            doc_id = table.insert(
                {
                    "guild_id": guild_id,
                    "name": name,
                    "support_role_ids": support_role_ids,
                    "category_id": category_id,
                    "transcript_channel_id": transcript_channel_id,
                    "panel_channel_id": panel_channel_id,
                    "message_id": None,
                    "button_label": button_label,
                }
            )
            table.update({"id": doc_id}, doc_ids=[doc_id])
            stored = self._first("panels", Query().id == doc_id)
            if stored is None:
                raise DatabaseError("Panel insert could not be read back")
            return Panel.from_doc(stored)

        return await self._run(op)

    async def get_panel(self, panel_id: int) -> Panel | None:
        def op() -> Panel | None:
            doc = self._first("panels", Query().id == panel_id)
            return Panel.from_doc(doc) if doc else None

        return await self._run(op)

    async def list_panels(self, guild_id: int | None = None) -> list[Panel]:
        def op() -> list[Panel]:
            table = self._tables["panels"]
            rows = table.all() if guild_id is None else table.search(Query().guild_id == guild_id)
            return [Panel.from_doc(r) for r in rows]

        return await self._run(op)

    async def update_panel(self, panel: Panel) -> Panel:
        def op() -> Panel:
            self._tables["panels"].update(panel.to_doc(), Query().id == panel.id)
            return panel

        return await self._run(op)

    async def delete_panel(self, panel_id: int) -> bool:
        def op() -> bool:
            return bool(self._tables["panels"].remove(Query().id == panel_id))

        return await self._run(op)

    async def create_ticket(
            self,
            *,
            guild_id: int,
            panel_id: int,
            channel_id: int,
            owner_id: int,
            number: int,
            control_message_id: int | None = None,
    ) -> Ticket:
        def op() -> Ticket:
            guilds = self._tables["guilds"]
            cond = Query().guild_id == guild_id
            doc = self._first("guilds", cond)
            current = int(doc.get("ticket_counter", 0)) if doc else 0

            if number != current + 1:
                raise DatabaseError(
                    f"Ticket counter mismatch for guild {guild_id}: "
                    f"expected {current + 1}, got {number}"
                )

            if doc is None:
                guilds.insert(GuildSettings(guild_id=guild_id, ticket_counter=number).to_doc())
            else:
                guilds.update({"ticket_counter": number}, cond)

            table = self._tables["tickets"]
            doc_id = table.insert(
                {
                    "guild_id": guild_id,
                    "panel_id": panel_id,
                    "channel_id": channel_id,
                    "owner_id": owner_id,
                    "number": number,
                    "status": TicketStatus.OPEN.value,
                    "claimed_by": None,
                    "created_at": utcnow_iso(),
                    "closed_at": None,
                    "transcript_path": None,
                    "control_message_id": control_message_id,
                    "close_reason": None,
                    "custom_name": None,
                }
            )
            table.update({"id": doc_id}, doc_ids=[doc_id])
            stored = self._first("tickets", Query().id == doc_id)
            if stored is None:
                raise DatabaseError("Ticket insert could not be read back")
            return Ticket.from_doc(stored)

        return await self._run(op)

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        def op() -> Ticket | None:
            doc = self._first("tickets", Query().id == ticket_id)
            return Ticket.from_doc(doc) if doc else None

        return await self._run(op)

    async def get_ticket_by_channel(self, channel_id: int) -> Ticket | None:
        def op() -> Ticket | None:
            rows = self._tables["tickets"].search(
                (Query().channel_id == channel_id) & (Query().status != TicketStatus.DELETED.value)
            )
            return Ticket.from_doc(rows[0]) if rows else None

        return await self._run(op)

    async def list_tickets(
            self,
            guild_id: int | None = None,
            *,
            owner_id: int | None = None,
            status: TicketStatus | None = None,
    ) -> list[Ticket]:
        def op() -> list[Ticket]:
            rows = self._tables["tickets"].all()
            result = [Ticket.from_doc(r) for r in rows]
            if guild_id is not None:
                result = [t for t in result if t.guild_id == guild_id]
            if owner_id is not None:
                result = [t for t in result if t.owner_id == owner_id]
            if status is not None:
                result = [t for t in result if t.status == status]
            return result

        return await self._run(op)

    async def modify_ticket(self, ticket_id: int, mutate: Callable[[Ticket], Ticket]) -> Ticket:
        def op() -> Ticket:
            table = self._tables["tickets"]
            cond = Query().id == ticket_id
            doc = self._first("tickets", cond)
            if doc is None:
                raise DatabaseError(f"Ticket {ticket_id} does not exist")
            updated = mutate(Ticket.from_doc(doc))
            table.update(updated.to_doc(), cond)
            return updated

        return await self._run(op)

    @staticmethod
    def _member_cond(ticket_id: int, user_id: int | None, role_id: int | None) -> Any:
        cond = Query().ticket_id == ticket_id
        if user_id is not None:
            return cond & (Query().user_id == user_id)
        return cond & (Query().role_id == role_id)

    async def set_ticket_member(
            self,
            ticket_id: int,
            *,
            user_id: int | None = None,
            role_id: int | None = None,
            blocked: bool = False,
    ) -> TicketMember:
        if (user_id is None) == (role_id is None):
            raise DatabaseError("Provide exactly one of user_id or role_id")

        def op() -> TicketMember:
            table = self._tables["ticket_members"]
            cond = self._member_cond(ticket_id, user_id, role_id)
            if self._first("ticket_members", cond):
                table.update({"blocked": blocked}, cond)
            else:
                table.insert(
                    TicketMember(
                        ticket_id=ticket_id, user_id=user_id, role_id=role_id, blocked=blocked
                    ).to_doc()
                )
            stored = self._first("ticket_members", cond)
            if stored is None:
                raise DatabaseError("Member upsert could not be read back")
            return TicketMember.from_doc(stored)

        return await self._run(op)

    async def list_ticket_members(self, ticket_id: int) -> list[TicketMember]:
        def op() -> list[TicketMember]:
            rows = self._tables["ticket_members"].search(Query().ticket_id == ticket_id)
            return [TicketMember.from_doc(r) for r in rows]

        return await self._run(op)

    async def remove_ticket_member(
            self, ticket_id: int, *, user_id: int | None = None, role_id: int | None = None
    ) -> bool:
        if (user_id is None) == (role_id is None):
            raise DatabaseError("Provide exactly one of user_id or role_id")

        def op() -> bool:
            cond = self._member_cond(ticket_id, user_id, role_id)
            return bool(self._tables["ticket_members"].remove(cond))

        return await self._run(op)

    async def is_blocked(
            self, ticket_id: int, *, user_id: int | None = None, role_id: int | None = None
    ) -> bool:
        if (user_id is None) == (role_id is None):
            raise DatabaseError("Provide exactly one of user_id or role_id")

        def op() -> bool:
            doc = self._first("ticket_members", self._member_cond(ticket_id, user_id, role_id))
            return bool(doc and doc.get("blocked"))

        return await self._run(op)
