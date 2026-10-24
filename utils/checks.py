from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import TYPE_CHECKING, Any, TypeVar, cast

import discord
from discord import app_commands

from utils.database import Ticket, TicketStatus

if TYPE_CHECKING:
    from bot import TicketBot

T = TypeVar("T")
Predicate = Callable[[discord.Interaction], Awaitable[bool]]


class PermissionLevel(IntEnum):
    USER = 0
    SUPPORT = 1
    ADMIN = 2
    OWNER = 3


def _bot(interaction: discord.Interaction) -> TicketBot:
    return cast("TicketBot", interaction.client)


def _member(interaction: discord.Interaction) -> discord.Member:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        raise app_commands.NoPrivateMessage("This command can only be used in a server.")
    return interaction.user


def is_admin_member(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild


def is_owner_id(bot: TicketBot, user_id: int) -> bool:
    return user_id in bot.settings.bot_owner_ids


async def has_support_access(
    bot: TicketBot, member: discord.Member, ticket: Ticket | None = None
) -> bool:
    if is_admin_member(member):
        return True
    role_ids = {role.id for role in member.roles}
    guild_settings = await bot.db.get_guild(member.guild.id)
    allowed = set(guild_settings.support_roles)
    if ticket is not None:
        panel = await bot.db.get_panel(ticket.panel_id)
        if panel is not None:
            allowed.update(panel.support_role_ids)
    return bool(role_ids & allowed)


async def get_permission_level(
    bot: TicketBot, member: discord.Member, ticket: Ticket | None = None
) -> PermissionLevel:
    if is_owner_id(bot, member.id):
        return PermissionLevel.OWNER
    if is_admin_member(member):
        return PermissionLevel.ADMIN
    if await has_support_access(bot, member, ticket):
        return PermissionLevel.SUPPORT
    return PermissionLevel.USER


def is_support() -> Callable[[T], T]:
    async def predicate(interaction: discord.Interaction) -> bool:
        member = _member(interaction)
        bot = _bot(interaction)
        ticket = (
            await bot.db.get_ticket_by_channel(interaction.channel_id)
            if interaction.channel_id
            else None
        )
        if await has_support_access(bot, member, ticket):
            return True
        raise app_commands.CheckFailure("You need a support role to use this command.")

    return app_commands.check(predicate)


def is_admin() -> Callable[[T], T]:
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin_member(_member(interaction)):
            return True
        raise app_commands.CheckFailure("You need the Manage Server permission.")

    return app_commands.check(predicate)


def is_bot_owner() -> Callable[[T], T]:
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_owner_id(_bot(interaction), interaction.user.id):
            return True
        raise app_commands.CheckFailure("Only the bot owner can use this command.")

    return app_commands.check(predicate)


def in_ticket() -> Callable[[T], T]:
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id is not None:
            ticket = await _bot(interaction).db.get_ticket_by_channel(interaction.channel_id)
            if ticket is not None and ticket.status != TicketStatus.DELETED:
                return True
        raise app_commands.CheckFailure("This command can only be used inside a ticket channel.")

    return app_commands.check(predicate)


def is_ticket_owner_or_support() -> Callable[[T], T]:
    async def predicate(interaction: discord.Interaction) -> bool:
        member = _member(interaction)
        bot = _bot(interaction)
        ticket = (
            await bot.db.get_ticket_by_channel(interaction.channel_id)
            if interaction.channel_id
            else None
        )
        if ticket is None:
            raise app_commands.CheckFailure(
                "This command can only be used inside a ticket channel."
            )
        if member.id == ticket.owner_id or await has_support_access(bot, member, ticket):
            return True
        raise app_commands.CheckFailure("Only the ticket owner or support can do that.")

    return app_commands.check(predicate)


def check_all(*decorators: Callable[[Any], Any]) -> Callable[[T], T]:
    def wrapper(func: T) -> T:
        for decorator in reversed(decorators):
            func = decorator(func)
        return func

    return wrapper
