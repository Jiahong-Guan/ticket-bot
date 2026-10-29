from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord


def make_role(role_id: int, *, default: bool = False) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.mention = f"<@&{role_id}>"
    role.is_default.return_value = default
    return role


def make_member(
    user_id: int, *, roles: tuple[int, ...] = (), manage_guild: bool = False, guild_id: int = 1
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.mention = f"<@{user_id}>"
    member.display_name = f"user{user_id}"
    member.roles = [make_role(r) for r in roles]
    member.guild_permissions = SimpleNamespace(manage_guild=manage_guild)
    member.guild = SimpleNamespace(id=guild_id)
    return member


def make_channel(channel_id: int = 500, guild: Any = None) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = "ticket-0001"
    channel.mention = f"<#{channel_id}>"
    channel.jump_url = f"https://discord.com/channels/1/{channel_id}"
    channel.guild = guild
    channel.overwrites = {}
    channel.category = None
    channel.set_permissions = AsyncMock()
    channel.edit = AsyncMock()
    return channel


def make_message(
    content: str,
    *,
    author_id: int = 7,
    attachments: tuple[tuple[str, str], ...] = (),
    embeds: tuple[tuple[str | None, str | None], ...] = (),
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.created_at = datetime(2026, 10, 24, 12, 30, 5, tzinfo=timezone.utc)
    message.author = SimpleNamespace(display_name=f"user{author_id}", id=author_id)
    message.clean_content = content
    message.system_content = ""
    message.attachments = [SimpleNamespace(filename=n, url=u) for n, u in attachments]
    message.embeds = [SimpleNamespace(title=t, description=d) for t, d in embeds]
    return message


async def async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def make_interaction(
    *, user: Any = None, guild: Any = None, channel_id: int | None = 500, client: Any = None
) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild = guild
    interaction.channel_id = channel_id
    interaction.client = client
    interaction.extras = {}
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction
