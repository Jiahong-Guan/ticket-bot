from __future__ import annotations

from enum import IntEnum

import discord

from utils.config import EmbedTheme


class EmbedColor(IntEnum):
    PRIMARY = 0x5865F2
    SUCCESS = 0x57F287
    ERROR = 0xED4245
    WARNING = 0xFEE75C
    CLOSED = 0x99AAB5


_overrides: dict[EmbedColor, int] = {}


def configure_theme(theme: EmbedTheme) -> None:
    _overrides.clear()
    _overrides[EmbedColor.PRIMARY] = theme.as_int("primary")
    _overrides[EmbedColor.SUCCESS] = theme.as_int("success")
    _overrides[EmbedColor.ERROR] = theme.as_int("error")
    _overrides[EmbedColor.WARNING] = theme.as_int("warning")


def resolve_color(color: EmbedColor) -> int:
    return _overrides.get(color, int(color))


def _base(
    color: EmbedColor,
    title: str | None,
    description: str | None,
) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        colour=resolve_color(color),
        timestamp=discord.utils.utcnow(),
    )


def success(description: str, *, title: str = "Success") -> discord.Embed:
    return _base(EmbedColor.SUCCESS, f"\u2705 {title}", description)


def error(description: str, *, title: str = "Error") -> discord.Embed:
    return _base(EmbedColor.ERROR, f"\u274c {title}", description)


def warning(description: str, *, title: str = "Warning") -> discord.Embed:
    return _base(EmbedColor.WARNING, f"\u26a0\ufe0f {title}", description)


def info(description: str, *, title: str | None = None) -> discord.Embed:
    return _base(EmbedColor.PRIMARY, title, description)


def ticket_created(
    *,
    owner_id: int,
    panel_name: str,
    number: int,
    claimed_by_id: int | None = None,
) -> discord.Embed:
    embed = _base(
        EmbedColor.PRIMARY,
        f"Ticket #{number:04d}",
        f"Welcome <@{owner_id}>! A support member will be with you shortly.\n"
        "Describe your issue in as much detail as you can.",
    )
    embed.add_field(name="Panel", value=panel_name, inline=True)
    embed.add_field(name="Owner", value=f"<@{owner_id}>", inline=True)
    embed.add_field(
        name="Claimed by",
        value=f"<@{claimed_by_id}>" if claimed_by_id else "Unclaimed",
        inline=True,
    )
    return embed


def ticket_closed(
    *,
    number: int,
    closed_by_id: int,
    reason: str | None,
    transcript_url: str | None = None,
    note: str | None = None,
) -> discord.Embed:
    embed = _base(
        EmbedColor.CLOSED,
        f"Ticket #{number:04d} closed",
        f"Closed by <@{closed_by_id}>.",
    )
    embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    if transcript_url:
        embed.add_field(name="Transcript", value=f"[View transcript]({transcript_url})")
    if note:
        embed.set_footer(text=note)
    return embed


def transcript(
    *,
    number: int,
    owner_id: int,
    closed_by_id: int,
    message_count: int,
    channel_name: str,
) -> discord.Embed:
    embed = _base(EmbedColor.PRIMARY, f"Transcript: ticket #{number:04d}", None)
    embed.add_field(name="Owner", value=f"<@{owner_id}>", inline=True)
    embed.add_field(name="Closed by", value=f"<@{closed_by_id}>", inline=True)
    embed.add_field(name="Messages", value=str(message_count), inline=True)
    embed.add_field(name="Channel", value=channel_name, inline=True)
    return embed


def panel_embed(panel_name: str) -> discord.Embed:
    embed = _base(
        EmbedColor.PRIMARY,
        "Support Tickets",
        "To create a ticket use the Create ticket button",
    )
    embed.timestamp = None
    embed.set_footer(text=panel_name)
    return embed
