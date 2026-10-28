from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from utils import embeds
from utils.checks import has_support_access, is_admin_member
from utils.database import Panel, Ticket, TicketStatus, utcnow_iso
from utils.exceptions import (
    ConfigurationError,
    PermissionDenied,
    TargetBlocked,
    TicketLimitReached,
    TicketStateError,
)
from utils.logger import get_logger
from utils.rate_limit import ChannelEditLimiter, humanize_seconds
from utils.transcript import AttachmentBudget, entry_from_message, render_transcript_html
from utils.views.ticket_controls import ClosedControlView, TicketControlView

if TYPE_CHECKING:
    from bot import TicketBot

log = get_logger("tickets.service")

_NAME_CLEAN = re.compile(r"[^a-z0-9_-]+")
_EDIT_TIMEOUT_SECONDS = 15.0
_MAX_EMBED_TEXT = 200


def sanitize_channel_name(raw: str) -> str:
    cleaned = _NAME_CLEAN.sub("-", raw.strip().lower()).strip("-_")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:100]
    if not cleaned:
        raise TicketStateError("That name has no valid characters. Use letters or numbers.")
    return cleaned


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _owner_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, send_messages=True, attach_files=True, read_message_history=True
    )


def _support_overwrite(manage_messages: bool) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        attach_files=True,
        read_message_history=True,
        manage_messages=manage_messages or None,
    )


# Everything that lets someone speak in (or around) a channel. Threads and reactions are
# included so a locked ticket cannot be talked in through a side door.
_LOCK_DENIES: dict[str, bool] = {
    "send_messages": False,
    "attach_files": False,
    "add_reactions": False,
    "send_messages_in_threads": False,
    "create_public_threads": False,
    "create_private_threads": False,
}


def _clone(overwrite: discord.PermissionOverwrite) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(**{k: v for k, v in overwrite if v is not None})


def is_locked(overwrite: discord.PermissionOverwrite) -> bool:
    return overwrite.view_channel is True and overwrite.send_messages is False


def lock_overwrites(
    overwrites: Mapping[Any, discord.PermissionOverwrite], *, protect_ids: set[int]
) -> dict[Any, discord.PermissionOverwrite]:
    result = dict(overwrites)
    for target, overwrite in overwrites.items():
        if not isinstance(target, (discord.Member, discord.Role)) or target.id in protect_ids:
            continue
        if overwrite.view_channel is not True:
            continue
        locked = _clone(overwrite)
        locked.update(view_channel=True, read_message_history=True, **_LOCK_DENIES)
        result[target] = locked
    return result


def unlock_overwrites(
    overwrites: Mapping[Any, discord.PermissionOverwrite],
    *,
    support_role_ids: set[int],
    manage_messages: bool,
) -> dict[Any, discord.PermissionOverwrite]:
    result = dict(overwrites)
    for target, overwrite in overwrites.items():
        if not isinstance(target, (discord.Member, discord.Role)) or not is_locked(overwrite):
            continue
        is_support_role = isinstance(target, discord.Role) and target.id in support_role_ids
        result[target] = (
            _support_overwrite(manage_messages) if is_support_role else _owner_overwrite()
        )
    return result


@dataclass(slots=True, frozen=True)
class CloseResult:
    ticket: Ticket
    transcript_url: str | None
    transcript_path: Path
    message_count: int
    renamed: bool = True


@dataclass(slots=True, frozen=True)
class ReopenResult:
    ticket: Ticket
    renamed: bool


class TicketService:
    def __init__(self, bot: TicketBot) -> None:
        self.bot = bot
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._edit_limiter = ChannelEditLimiter()

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    def _open_name(self, ticket: Ticket) -> str:
        return ticket.custom_name or self.bot.settings.ticket_format.open.format(
            number=ticket.number
        )

    async def _enforce_limit(
            self, guild: discord.Guild, owner_id: int, ignore_ticket_id: int | None = None
    ) -> None:
        limit = self.bot.settings.max_open_tickets_per_user
        existing = await self.bot.db.list_tickets(
            guild.id, owner_id=owner_id, status=TicketStatus.OPEN
        )
        live: list[discord.abc.GuildChannel] = []
        for ticket in existing:
            if ticket.id == ignore_ticket_id:
                continue
            channel = guild.get_channel(ticket.channel_id)
            if channel is None:
                log.warning(f"Ticket {ticket.id} has no channel; marking deleted")
                await self.bot.db.modify_ticket(
                    ticket.id,
                    lambda t: replace(t, status=TicketStatus.DELETED, closed_at=utcnow_iso()),
                )
                continue
            live.append(channel)
        if len(live) >= limit:
            raise TicketLimitReached(live[0].jump_url, limit)

    async def _guarded_edit(self, channel: discord.TextChannel, **changes: Any) -> bool:
        wait = self._edit_limiter.retry_after(channel.id)
        if wait > 0 or not self._edit_limiter.acquire(channel.id):
            log.warning(
                f"Skipping edit of channel {channel.id}: rename budget spent (retry in {humanize_seconds(wait)})"
            )
            return False
        try:
            await asyncio.wait_for(channel.edit(**changes), timeout=_EDIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.warning(f"Edit of channel {channel.id} stalled (likely rate limited); skipped", )
            return False
        return True

    async def _support_role_ids(self, guild_id: int, panel: Panel | None) -> set[int]:
        settings = await self.bot.db.get_guild(guild_id)
        return {*settings.support_roles, *(panel.support_role_ids if panel else [])}

    async def _panel_name(self, ticket: Ticket) -> str:
        panel = await self.bot.db.get_panel(ticket.panel_id)
        return panel.name if panel else "Unknown panel"

    async def _refresh_control_message(self, channel: discord.TextChannel, ticket: Ticket) -> None:
        if ticket.control_message_id is None:
            return
        try:
            await channel.get_partial_message(ticket.control_message_id).edit(
                embed=embeds.ticket_created(
                    owner_id=ticket.owner_id,
                    panel_name=await self._panel_name(ticket),
                    number=ticket.number,
                    claimed_by_id=ticket.claimed_by,
                )
            )
        except discord.HTTPException:
            log.exception(f"Could not refresh control message of ticket {ticket.id}")

    @staticmethod
    def _topic(ticket: Ticket, claimer_name: str | None) -> str:
        return (
            f"Ticket #{ticket.number:04d} | Owner ID: {ticket.owner_id} | "
            f"Claimed by: {claimer_name or 'nobody'}"
        )

    async def create_ticket(
        self, guild: discord.Guild, owner: discord.Member, panel: Panel
    ) -> tuple[Ticket, discord.TextChannel]:
        async with self._lock_for(guild.id):
            await self._enforce_limit(guild, owner.id)
            category = guild.get_channel(panel.category_id)
            if not isinstance(category, discord.CategoryChannel):
                raise ConfigurationError(
                    "The ticket category for this panel no longer exists. "
                    "Ask an admin to run /setup."
                )
            number = await self.bot.db.peek_ticket_number(guild.id)
            name = self.bot.settings.ticket_format.open.format(number=number)
            guild_settings = await self.bot.db.get_guild(guild.id)

            bot_member = guild.me or guild.get_member(self.bot.user.id)
            if bot_member is None:
                bot_member = await guild.fetch_member(self.bot.user.id)

            overwrites: dict[
                discord.Role | discord.Member, discord.PermissionOverwrite
            ] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                owner: _owner_overwrite(),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True,
                    manage_permissions=True,
                    embed_links=True,
                    attach_files=True,
                    read_message_history=True,
                ),
            }
            support = _support_overwrite(self.bot.settings.support_can_manage_messages)
            support_roles: list[discord.Role] = []
            for role_id in {*panel.support_role_ids, *guild_settings.support_roles}:
                role = guild.get_role(role_id)
                if role is None or role.is_default():
                    continue
                overwrites[role] = support
                support_roles.append(role)

            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket #{number:04d} | Owner ID: {owner.id} | Claimed by: nobody",
                reason=f"Ticket #{number:04d} opened by {owner} ({owner.id})",
            )
            try:
                message = await channel.send(
                    content=" ".join([owner.mention, *(role.mention for role in support_roles)]),
                    embed=embeds.ticket_created(
                        owner_id=owner.id, panel_name=panel.name, number=number
                    ),
                    view=TicketControlView(self.bot),
                    allowed_mentions=discord.AllowedMentions(
                        users=[owner], roles=support_roles
                    ),
                )
                ticket = await self.bot.db.create_ticket(
                    guild_id=guild.id,
                    panel_id=panel.id,
                    channel_id=channel.id,
                    owner_id=owner.id,
                    number=number,
                    control_message_id=message.id,
                )
            except Exception:
                log.exception("Ticket creation failed after channel creation; cleaning up")
                with contextlib.suppress(discord.HTTPException):
                    await channel.delete(reason=f"Ticket #{number:04d} creation failed")
                raise
            try:
                await message.pin(reason="Ticket controls")
            except discord.HTTPException:
                log.warning(f"Could not pin welcome message in {channel.id}", exc_info=True)
            log.info(f"Ticket #{number:04d} created in guild {guild.id} by {owner.id}")
            return ticket, channel

    async def write_transcript(
        self, channel: discord.TextChannel, ticket: Ticket
    ) -> tuple[Path, int]:
        budget = AttachmentBudget()
        entries = [
            await entry_from_message(m, budget)
            async for m in channel.history(limit=None, oldest_first=True)
        ]
        html_text = render_transcript_html(
            guild_name=channel.guild.name,
            guild_id=channel.guild.id,
            channel_name=channel.name,
            channel_id=channel.id,
            ticket_number=ticket.number,
            owner_id=ticket.owner_id,
            entries=entries,
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = (
                self.bot.settings.transcript_path
                / str(channel.guild.id)
                / f"{ticket.number:04d}-{stamp}.html"
        )
        await asyncio.to_thread(_write_text, path, html_text)
        return path, len(entries)

    async def _post_transcript(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        panel: Panel | None,
        closed_by: discord.Member,
        path: Path,
        count: int,
    ) -> str | None:
        guild_settings = await self.bot.db.get_guild(channel.guild.id)
        target_id = panel.transcript_channel_id if panel else guild_settings.transcript_channel
        target = channel.guild.get_channel(target_id) if target_id else None
        if not isinstance(target, discord.TextChannel):
            log.warning(f"No usable transcript channel for ticket {ticket.id}")
            return None
        try:
            sent = await target.send(
                embed=embeds.transcript(
                    number=ticket.number,
                    owner_id=ticket.owner_id,
                    closed_by_id=closed_by.id,
                    message_count=count,
                    channel_name=channel.name,
                ),
                file=discord.File(path, filename=path.name),
            )
        except discord.HTTPException:
            log.exception(f"Could not upload transcript for ticket {ticket.id}")
            return None
        return sent.jump_url

    async def close_ticket(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        closed_by: discord.Member,
        reason: str | None,
    ) -> CloseResult:
        """
        Close a ticket.

        Order matters. First everyone is made read-only (a permission edit, which is not
        subject to the name rate limit) so no message can slip in after the transcript. Then the
        transcript is written and uploaded. Last, the channel is renamed and moved; the rename is
        budgeted and skipped if Discord would rate limit it.

        Raises:
            TicketStateError: If the ticket is not open.
            PermissionDenied: If ``closed_by`` is neither the owner nor support.
        """
        if ticket.status != TicketStatus.OPEN:
            raise TicketStateError("This ticket is already closed.")
        if closed_by.id != ticket.owner_id and not await has_support_access(
                self.bot, closed_by, ticket
        ):
            raise PermissionDenied("Only the ticket owner or support can close this ticket.")

        guild = channel.guild
        panel = await self.bot.db.get_panel(ticket.panel_id)
        original = dict(channel.overwrites)
        locked = lock_overwrites(original, protect_ids={guild.me.id, guild.default_role.id})
        await channel.edit(
            overwrites=locked, reason=f"Ticket closed by {closed_by} ({closed_by.id})"
        )

        try:
            path, count = await self.write_transcript(channel, ticket)
            url = await self._post_transcript(channel, ticket, panel, closed_by, path, count)
        except Exception:
            log.exception(f"Closing ticket {ticket.id} failed; restoring permissions")
            with contextlib.suppress(discord.HTTPException):
                await channel.edit(overwrites=original, reason="Close failed; restoring access")
            raise

        guild_settings = await self.bot.db.get_guild(guild.id)
        closed_category = (
            guild.get_channel(guild_settings.closed_category)
            if guild_settings.closed_category
            else None
        )
        target_category = (
            closed_category if isinstance(closed_category, discord.CategoryChannel) else None
        )
        renamed = await self._guarded_edit(
            channel,
            name=self.bot.settings.ticket_format.closed.format(number=ticket.number),
            category=target_category or channel.category,
            reason=f"Ticket closed by {closed_by} ({closed_by.id})",
        )
        if not renamed and target_category is not None:
            await channel.edit(category=target_category, reason="Moving closed ticket")

        updated = await self.bot.db.modify_ticket(
            ticket.id,
            lambda t: replace(
                t,
                status=TicketStatus.CLOSED,
                closed_at=utcnow_iso(),
                transcript_path=str(path),
                close_reason=reason,
            ),
        )
        log.info(f"Ticket {ticket.id} closed by {closed_by.id} (renamed={renamed})")
        return CloseResult(updated, url, path, count, renamed)

    async def close_via_interaction(
        self, interaction: discord.Interaction, ticket: Ticket, reason: str | None
    ) -> None:
        channel = interaction.channel
        member = interaction.user
        if not isinstance(channel, discord.TextChannel) or not isinstance(member, discord.Member):
            raise TicketStateError("Tickets can only be closed from a server text channel.")
        result = await self.close_ticket(channel, ticket, member, reason)
        embed = embeds.ticket_closed(
            number=ticket.number,
            closed_by_id=member.id,
            reason=reason,
            transcript_url=result.transcript_url,
            note=None if result.renamed else "Channel rename skipped: Discord rate limit.",
        )
        view = ClosedControlView(self.bot)
        if result.transcript_url is None:
            try:
                await interaction.followup.send(
                    embed=embed, view=view, file=discord.File(result.transcript_path)
                )
                return
            except discord.HTTPException:
                log.exception(f"Could not attach transcript to closed ticket {ticket.id}")
        await interaction.followup.send(embed=embed, view=view)

    async def reopen_ticket(
        self, channel: discord.TextChannel, ticket: Ticket, reopened_by: discord.Member
    ) -> ReopenResult:
        if ticket.status != TicketStatus.CLOSED:
            raise TicketStateError("This ticket is not closed.")
        if not await has_support_access(self.bot, reopened_by, ticket):
            raise PermissionDenied("Only support staff can reopen tickets.")
        guild = channel.guild
        async with self._lock_for(guild.id):
            await self._enforce_limit(guild, ticket.owner_id, ignore_ticket_id=ticket.id)
            panel = await self.bot.db.get_panel(ticket.panel_id)
            restored = unlock_overwrites(
                channel.overwrites,
                support_role_ids=await self._support_role_ids(guild.id, panel),
                manage_messages=self.bot.settings.support_can_manage_messages,
            )
            category = guild.get_channel(panel.category_id) if panel else None
            await channel.edit(
                overwrites=restored,
                category=category
                if isinstance(category, discord.CategoryChannel)
                else channel.category,
                reason=f"Ticket reopened by {reopened_by} ({reopened_by.id})",
            )
            renamed = await self._guarded_edit(
                channel,
                name=self._open_name(ticket),
                reason=f"Ticket reopened by {reopened_by} ({reopened_by.id})",
            )
            updated = await self.bot.db.modify_ticket(
                ticket.id,
                lambda t: replace(t, status=TicketStatus.OPEN, closed_at=None, close_reason=None),
            )
        log.info(f"Ticket {ticket.id} reopened by {reopened_by.id} (renamed={renamed})")
        return ReopenResult(updated, renamed)

    async def delete_ticket(
        self, channel: discord.TextChannel, ticket: Ticket, deleted_by: discord.Member
    ) -> None:
        if not await has_support_access(self.bot, deleted_by, ticket):
            raise PermissionDenied("Only support staff can delete tickets.")
        await channel.delete(reason=f"Ticket deleted by {deleted_by} ({deleted_by.id})")
        await self.bot.db.modify_ticket(
            ticket.id,
            lambda t: replace(
                t, status=TicketStatus.DELETED, closed_at=t.closed_at or utcnow_iso()
            ),
        )
        log.info(f"Ticket {ticket.id} deleted by {deleted_by.id}")

    async def toggle_claim(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        invoker: discord.Member,
        target: discord.Member | None,
    ) -> Ticket:
        if ticket.status != TicketStatus.OPEN:
            raise TicketStateError("Only open tickets can be claimed.")
        target = target or invoker
        if not await has_support_access(self.bot, invoker, ticket):
            raise PermissionDenied("Only support staff can claim tickets.")
        if not await has_support_access(self.bot, target, ticket):
            raise TicketStateError(f"{target.display_name} is not a support member.")

        can_override = invoker.id == ticket.claimed_by or is_admin_member(invoker)
        new_owner: int | None
        if ticket.claimed_by is None:
            new_owner = target.id
        elif ticket.claimed_by == target.id:
            if not can_override:
                raise PermissionDenied("Only the claimer or an admin can release this claim.")
            new_owner = None
        else:
            if not can_override:
                raise TicketStateError(f"Already claimed by <@{ticket.claimed_by}>.")
            new_owner = target.id

        updated = await self.bot.db.modify_ticket(
            ticket.id, lambda t: replace(t, claimed_by=new_owner)
        )

        # The topic is cosmetic and if the edit budget is spent the claim still stands.
        await self._guarded_edit(
            channel,
            topic=self._topic(updated, target.display_name if new_owner else None),
            reason=f"Claim changed by {invoker} ({invoker.id})",
        )
        await self._refresh_control_message(channel, updated)
        return updated

    async def rename_ticket(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        invoker: discord.Member,
        name: str | None,
    ) -> str:
        if ticket.status != TicketStatus.OPEN:
            raise TicketStateError("Only open tickets can be renamed.")
        if not await has_support_access(self.bot, invoker, ticket):
            raise PermissionDenied("Only support staff can rename tickets.")
        custom = sanitize_channel_name(name) if name else None
        wait = self._edit_limiter.retry_after(channel.id)
        if wait > 0:
            raise TicketStateError(
                "Discord only allows 2 channel renames per 10 minutes. "
                f"Try again in {humanize_seconds(wait)}.",
            )
        new_name = custom or self.bot.settings.ticket_format.open.format(number=ticket.number)
        if not await self._guarded_edit(
                channel, name=new_name, reason=f"Renamed by {invoker} ({invoker.id})"
        ):
            raise TicketStateError("Discord is rate limiting renames right now. Try again later.")
        await self.bot.db.modify_ticket(ticket.id, lambda t: replace(t, custom_name=custom))
        return new_name

    async def add_targets(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        invoker: discord.Member,
        user: discord.Member | None,
        role: discord.Role | None,
    ) -> list[str]:
        if user is None and role is None:
            raise TicketStateError("Provide a user, a role or both.")
        if ticket.status != TicketStatus.OPEN:
            raise TicketStateError("People can only be added to open tickets.")
        if not await has_support_access(self.bot, invoker, ticket):
            raise PermissionDenied("Only support staff can add people to tickets.")
        if role is not None and role.is_default():
            raise TicketStateError("You cannot add @everyone to a ticket.")

        mentions: list[str] = []
        for target in (user, role):
            if target is None:
                continue
            is_user = isinstance(target, discord.Member)
            blocked = (
                await self.bot.db.is_blocked(ticket.id, user_id=target.id)
                if is_user
                else await self.bot.db.is_blocked(ticket.id, role_id=target.id)
            )
            if blocked and not is_admin_member(invoker):
                raise TargetBlocked()
            await channel.set_permissions(
                target, overwrite=_owner_overwrite(), reason=f"Added by {invoker} ({invoker.id})"
            )
            if is_user:
                await self.bot.db.set_ticket_member(ticket.id, user_id=target.id)
            else:
                await self.bot.db.set_ticket_member(ticket.id, role_id=target.id)
            mentions.append(target.mention)
        return mentions

    async def remove_targets(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        invoker: discord.Member,
        user: discord.Member | None,
        role: discord.Role | None,
        *,
        revoke: bool,
    ) -> list[str]:
        if user is None and role is None:
            raise TicketStateError("Provide a user, a role, or both.")
        if not await has_support_access(self.bot, invoker, ticket):
            raise PermissionDenied("Only support staff can remove people from tickets.")
        if user is not None and user.id == ticket.owner_id:
            raise TicketStateError("The ticket owner cannot be removed.")
        if role is not None and role.is_default():
            raise TicketStateError("You cannot remove @everyone from a ticket.")

        mentions: list[str] = []
        for target in (user, role):
            if target is None:
                continue

            if not isinstance(target, (discord.Member, discord.Role)):
                continue

            is_user = isinstance(target, discord.Member)
            reason = f"Removed by {invoker} ({invoker.id})"
            if revoke:
                await channel.set_permissions(
                    target,
                    overwrite=discord.PermissionOverwrite(view_channel=False),
                    reason=reason
                )
                if is_user:
                    await self.bot.db.set_ticket_member(ticket.id, user_id=target.id, blocked=True)
                else:
                    await self.bot.db.set_ticket_member(ticket.id, role_id=target.id, blocked=True)
            else:
                await channel.set_permissions(
                    target,
                    overwrite=None,
                    reason=reason
                )
                if is_user:
                    await self.bot.db.remove_ticket_member(ticket.id, user_id=target.id)
                else:
                    await self.bot.db.remove_ticket_member(ticket.id, role_id=target.id)
            mentions.append(target.mention)
        return mentions


