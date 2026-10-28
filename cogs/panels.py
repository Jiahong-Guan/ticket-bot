from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from cogs.base_command import BaseCog
from utils.checks import is_admin
from utils.views.panel_wizard import PanelWizardView, WizardState

if TYPE_CHECKING:
    from bot import TicketBot


class PanelsCog(BaseCog):
    @app_commands.command(name="setup", description="Create a ticket panel with a guided wizard.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @is_admin()
    async def setup_panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return

        state = WizardState(owner_id=interaction.user.id, guild_id=guild.id)
        view = PanelWizardView(self.bot, state)
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        view.origin = interaction


async def setup(bot: TicketBot) -> None:
    await bot.add_cog(PanelsCog(bot))
