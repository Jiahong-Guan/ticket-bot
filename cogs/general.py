from __future__ import annotations

import platform
import sys
from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from cogs.base_command import BaseCog
from utils import VERSION, embeds
from utils.checks import PermissionLevel, get_permission_level, is_admin, is_bot_owner
from utils.logger import host_name

if TYPE_CHECKING:
    from bot import TicketBot

_FIELD_LIMIT = 1000


def collect_commands(
    commands: Sequence[Any],
    prefix: str = ""
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for cmd in commands:
        if isinstance(cmd, app_commands.Group):
            rows.extend(collect_commands(cmd.commands, f"{prefix}{cmd.name} "))
        elif isinstance(cmd, app_commands.Command):
            binding = cmd.binding
            section = binding.qualified_name if binding is not None else "General"
            rows.append((section, f"/{prefix}{cmd.name}", cmd.description))
    return rows


def format_uptime(delta_seconds: int) -> str:
    days, rem = divmod(delta_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


class GeneralCog(BaseCog):
    @app_commands.command(name="ping", description="Show the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=embeds.info(
                f"Gateway latency: **`{self.bot.latency * 1000:.0f} ms`**",
                title="Pong!"
            ),
            ephemeral=True
        )

    @app_commands.command(name="help", description="List every command.")
    async def help(self, interaction: discord.Interaction) -> None:
        rows = collect_commands(self.bot.tree.get_commands())
        grouped: dict[str, list[str]] = defaultdict(list)
        for section, usage, description in sorted(rows, key=lambda r: (r[0], r[1])):
            grouped[section].append(f"`{usage}` {description}")
        embed = embeds.info("Everything this bot can do.", title="Help")
        for section, lines in grouped.items():
            chunk = ""
            part = 1
            for line in lines:
                if len(chunk) + len(line) + 1 > _FIELD_LIMIT:
                    embed.add_field(name=f"{section} ({part})", value=chunk, inline=False)
                    chunk, part = "", part + 1
                chunk += line + "\n"
            embed.add_field(
                name=section if part == 1 else f"{section} ({part})", value=chunk, inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="debug", description="Show runtime diagnostics.")
    @app_commands.guild_only()
    @is_admin()
    async def debug(self, interaction: discord.Interaction) -> None:
        uptime = int((discord.utils.utcnow() - self.bot.started_at).total_seconds())
        embed = embeds.info("Runtime diagnostics.", title="Debug")
        embed.add_field(name="Bot version", value=VERSION)
        embed.add_field(name="Python", value=sys.version.split()[0])
        embed.add_field(name="discord.py", value=discord.__version__)
        embed.add_field(name="Host", value=f"{host_name()} ({platform.machine()})")
        embed.add_field(name="Uptime", value=format_uptime(uptime))
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f} ms")
        embed.add_field(name="Guilds", value=str(len(self.bot.guilds)))
        embed.add_field(name="Synced commands", value=str(self.bot.synced_commands))
        embed.add_field(
            name="Cogs",
            value=f"{len(self.bot.loaded_cogs)} loaded, {len(self.bot.failed_cogs)} failed",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="permissionlevel", description="Show a member's permission level.")
    @app_commands.describe(user="Member to inspect (defaults to you)")
    @app_commands.guild_only()
    @is_bot_owner()
    async def permissionlevel(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        target = user or self.get_member(interaction)
        ticket = (
            await self.db.get_ticket_by_channel(interaction.channel_id)
            if interaction.channel_id
            else None
        )
        level = await get_permission_level(self.bot, target, ticket)
        descriptions = {
            PermissionLevel.OWNER: "Listed in `bot_owner_ids`.",
            PermissionLevel.ADMIN: "Has the Manage Server permission.",
            PermissionLevel.SUPPORT: "Holds a support role.",
            PermissionLevel.USER: "No special permissions.",
        }
        await interaction.response.send_message(
            embed=embeds.info(
                f"{target.mention} is **{level.name.title()}** (level {int(level)}).\n"
                f"{descriptions[level]}",
                title="Permission level",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="id", description="Show Discord ids.")
    @app_commands.describe(
        user="User to look up",
        role="Role to look up",
        channel="Channel to look up",
    )
    async def id(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        role: discord.Role | None = None,
        channel: discord.abc.GuildChannel | None = None,
    ) -> None:
        embed = embeds.info("Copy any id below.", title="IDs")
        if user is None and role is None and channel is None:
            embed.add_field(name="You", value=f"`{interaction.user.id}`")
            if interaction.guild:
                embed.add_field(name="Server", value=f"`{interaction.guild.id}`")
            if interaction.channel_id:
                embed.add_field(name="This channel", value=f"`{interaction.channel_id}`")
        else:
            if user:
                embed.add_field(name=f"User {user.display_name}", value=f"`{user.id}`")
            if role:
                embed.add_field(name=f"Role {role.name}", value=f"`{role.id}`")
            if channel:
                embed.add_field(name=f"Channel {channel.name}", value=f"`{channel.id}`")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: TicketBot) -> None:
    await bot.add_cog(GeneralCog(bot))
