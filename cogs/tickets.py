from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from cogs.base_command import BaseCog
from utils import embeds
from utils.checks import in_ticket, is_support, is_ticket_owner_or_support
from utils.views.ticket_controls import DeleteTicketModal

if TYPE_CHECKING:
    from bot import TicketBot


class TicketsCog(BaseCog):
    @app_commands.command(name="add", description="Add a user or role to a ticket.")
    @app_commands.describe(
        user="User to add",
        role="Role to add",
        ticket="Ticket channel (default to this channel)"
    )
    @app_commands.guild_only()
    @is_support()
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        role: discord.Role | None = None,
        ticket: discord.TextChannel | None = None,
    ):
        row, channel = await self.resolve_ticket(interaction, ticket)
        await interaction.response.defer(ephemeral=channel.id != interaction.channel_id)
        mentions = await self.bot.tickets.add_targets(
            channel, row, self.get_member(interaction), user, role
        )
        await interaction.followup.send(
            embed=embeds.success(
                f"Added {', '.join(mentions)} to {channel.mention}.", title="Access Granted"
            )
        )

    @app_commands.command(name="remove", description="Remove a user or role from a ticket.")
    @app_commands.describe(
        user="User to remove",
        role="Role to remove",
        ticket="Ticket channel (defaults to this channel)",
        revoke="Also block them from being re-added (Manage Server can override"
    )
    @app_commands.guild_only()
    @is_support()
    async def remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        role: discord.Role | None = None,
        ticket: discord.TextChannel | None = None,
        revoke: bool = False,
    ) -> None:
        row, channel = await self.resolve_ticket(interaction, ticket)
        await interaction.response.defer(ephemeral=channel.id != interaction.channel_id)
        mentions = await self.bot.tickets.remove_targets(
            channel, row, self.get_member(interaction), user, role, revoke=revoke
        )
        suffix = " They are blocked from being re-added." if revoke else ""
        await interaction.followup.send(
            embed=embeds.success(
                f"Removed {', '.join(mentions)} from {channel.mention}.{suffix}",
                title="Access Removed",
            )
        )

    @app_commands.command(name="open", description="Reopen a closed ticket.")
    @app_commands.guild_only()
    @in_ticket()
    @is_support()
    async def open(self, interaction: discord.Interaction) -> None:
        row, channel = await self.resolve_ticket(interaction)
        await interaction.response.defer()
        result = await self.bot.tickets.reopen_ticket(channel, row, self.get_member(interaction))
        text = f"Ticket #{row.number:04d} was reopened. The owner can write again."
        if not result.renamed:
            text += "\nThe channel rename was skipped because of Discord's rate limit."
        await interaction.followup.send(embed=embeds.success(text, title="Reopened"))

    @app_commands.command(name="close", description="Close a ticket and save a transcript.")
    @app_commands.describe(reason="Why the ticket is being closed")
    @app_commands.guild_only()
    @is_ticket_owner_or_support()
    async def close(
        self,
        interaction: discord.Interaction,
        reason: app_commands.Range[str, 1, 500] | None = None,
    ) -> None:
        row, _ = await self.resolve_ticket(interaction)
        await interaction.response.defer()
        await self.bot.tickets.close_via_interaction(interaction, row, reason)

    @app_commands.command(name="delete", description="Permanently delete a ticket channel.")
    @app_commands.guild_only()
    @in_ticket()
    @is_support()
    async def delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DeleteTicketModal(self.bot))

    @app_commands.command(name="rename", description="Rename a ticket channel.")
    @app_commands.describe(name="New name (leave empty to restore the default ticket-XXXX)")
    @app_commands.guild_only()
    @in_ticket()
    @is_support()
    async def rename(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 100] | None = None
    ) -> None:
        row, channel = await self.resolve_ticket(interaction)
        await interaction.response.defer()
        new_name = await self.bot.tickets.rename_ticket(
            channel, row, self.get_member(interaction), name
        )
        await interaction.followup.send(
            embed=embeds.success(f"Ticket renamed to `{new_name}`.", title="Renamed")
        )

    @app_commands.command(name="claim", description="Claim or release this ticket.")
    @app_commands.describe(
        user="Claim on behalf of this support member (defaults to you)",
        reason="Optional note shown with the claim",
    )
    @app_commands.guild_only()
    @in_ticket()
    @is_support()
    async def claim(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        reason: app_commands.Range[str, 1, 300] | None = None,
    ) -> None:
        row, channel = await self.resolve_ticket(interaction)
        await interaction.response.defer()
        updated = await self.bot.tickets.toggle_claim(
            channel, row, self.get_member(interaction), user
        )
        text = (
            f"Claimed by <@{updated.claimed_by}>."
            if updated.claimed_by
            else "This ticket is no longer claimed."
        )
        embed = embeds.info(text, title="Claim updated")
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.followup.send(embed=embed)


async def setup(bot: TicketBot) -> None:
    await bot.add_cog(TicketsCog(bot))
