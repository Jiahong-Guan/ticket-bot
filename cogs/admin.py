from __future__ import annotations

import contextlib
from dataclasses import replace
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from cogs.base_command import BaseCog
from utils import embeds
from utils.checks import is_admin
from utils.database import GuildSettings
from utils.exceptions import BotError

if TYPE_CHECKING:
    from bot import TicketBot


class AdminCog(BaseCog):
    admin = app_commands.Group(
        name="admin",
        description="Ticket bot administration.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @admin.command(name="config", description="Show this server's ticket configuration.")
    @is_admin()
    async def show_config(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return

        cfg = await self.db.get_guild(guild.id)
        panels = await self.db.list_panels(guild.id)
        embed = embeds.info("Current configuration for this server.", title="Ticket configuration")
        embed.add_field(
            name="Support roles",
            value=", ".join(f"<@&{r}>" for r in cfg.support_roles) or "None",
            inline=False,
        )
        embed.add_field(
            name="Ticket category",
            value=f"<#{cfg.ticket_category}>" if cfg.ticket_category else "Not set",
        )
        embed.add_field(
            name="Closed category",
            value=f"<#{cfg.closed_category}>" if cfg.closed_category else "Not set",
        )
        embed.add_field(
            name="Transcript channel",
            value=f"<#{cfg.transcript_channel}>" if cfg.transcript_channel else "Not set",
        )
        embed.add_field(name="Panels", value=str(len(panels)))
        embed.add_field(name="Tickets created", value=str(cfg.ticket_counter))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="add-support-role", description="Add a server-wide support role.")
    @app_commands.describe(role="Role that may answer tickets")
    @is_admin()
    async def add_support_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if role.is_default():
            raise BotError("@everyone cannot be a support role.")

        def mutate(cfg: GuildSettings) -> GuildSettings:
            return replace(cfg, support_roles=sorted({*cfg.support_roles, role.id}))

        await self.db.modify_guild(guild.id, mutate)
        await interaction.response.send_message(
            embed=embeds.success(
                f"{role.mention} is now a support role. It applies to tickets created from now on.",
                title="Support role added",
            ),
            ephemeral=True,
        )

    @admin.command(name="remove-support-role", description="Remove a server-wide support role.")
    @app_commands.describe(role="Role to remove")
    @is_admin()
    async def remove_support_role(
            self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def mutate(cfg: GuildSettings) -> GuildSettings:
            return replace(cfg, support_roles=[r for r in cfg.support_roles if r != role.id])

        await self.db.modify_guild(guild.id, mutate)
        await interaction.response.send_message(
            embed=embeds.success(f"{role.mention} is no longer a server-wide support role."),
            ephemeral=True,
        )

    @admin.command(
        name="closed-category", description="Set (or clear) the category closed tickets move to."
    )
    @app_commands.describe(category="Category for closed tickets; omit to clear")
    @is_admin()
    async def closed_category(
            self,
            interaction: discord.Interaction,
            category: discord.CategoryChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def mutate(cfg: GuildSettings) -> GuildSettings:
            return replace(cfg, closed_category=category.id if category else None)

        await self.db.modify_guild(guild.id, mutate)
        text = (
            f"Closed tickets now move to **{category.name}**."
            if category
            else "Closed tickets stay in their original category."
        )
        await interaction.response.send_message(embed=embeds.success(text), ephemeral=True)

    @admin.command(name="panels", description="List this server's ticket panels.")
    @is_admin()
    async def list_panels(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        panels = await self.db.list_panels(guild.id)
        if not panels:
            await interaction.response.send_message(
                embed=embeds.info("No panels yet. Run /setup to create one."), ephemeral=True
            )
            return
        lines = [f"`{p.id}` **{p.name}** in <#{p.panel_channel_id}>" for p in panels]
        await interaction.response.send_message(
            embed=embeds.info("\n".join(lines)[:4000], title="Ticket panels"), ephemeral=True
        )

    @admin.command(
        name="delete-panel",
        description="Delete a panel and its message. Existing tickets are kept.",
    )
    @app_commands.describe(panel_id="Panel id from /admin panels")
    @is_admin()
    async def delete_panel(self, interaction: discord.Interaction, panel_id: int) -> None:
        guild = interaction.guild
        if guild is None:
            return
        panel = await self.db.get_panel(panel_id)
        if panel is None or panel.guild_id != guild.id:
            raise BotError("No panel with that id exists in this server.", title="Panel not found")
        await interaction.response.defer(ephemeral=True)
        channel = guild.get_channel(panel.panel_channel_id)
        if isinstance(channel, discord.TextChannel) and panel.message_id:
            with contextlib.suppress(discord.NotFound):
                await channel.get_partial_message(panel.message_id).delete()
        await self.db.delete_panel(panel.id)
        self.log.info(f"Panel {panel.id} deleted by {interaction.user.id}")
        await interaction.followup.send(
            embed=embeds.success(f"Panel **{panel.name}** was deleted.", title="Panel deleted"),
            ephemeral=True,
        )


async def setup(bot: TicketBot) -> None:
    await bot.add_cog(AdminCog(bot))
