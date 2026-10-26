from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord._types import ClientT
from discord.ext import commands

from utils.database import Database, Ticket
from utils.error_handler import handle_interaction_error
from utils.exceptions import PermissionDenied, TicketNotFound
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot import TicketBot


class BaseCog(commands.Cog):
    def __init__(self, bot: TicketBot) -> None:
        self.bot = bot
        self.log: logging.Logger = get_logger(f"cogs.{type(self).__name__.lower()}")

    @property
    def db(self) -> Database:
        return self.bot.db

    async def cog_load(self) -> None:
        self.log.info(f"{type(self).__name__} loaded")

    async def cog_unload(self) -> None:
        self.log.info(f"{type(self).__name__} unloaded")

    async def cog_app_command_error(
        self, interaction: discord.Interaction[ClientT], error: app_commands.AppCommandError
    ) -> None:
        await handle_interaction_error(interaction, error, self.log)

    @staticmethod
    def get_member(interaction: discord.Interaction) -> discord.Member:
        if not isinstance(interaction.user, discord.Member):
            raise PermissionDenied("This command can only be used in a server.")
        return interaction.user

    async def resolve_ticket(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> tuple[Ticket, discord.TextChannel]:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise TicketNotFound()
        ticket = await self.db.get_ticket_by_channel(target.id)
        if ticket is None:
            raise TicketNotFound()
        return ticket, target
