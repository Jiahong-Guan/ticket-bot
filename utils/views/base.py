from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord import Interaction
from discord._types import ClientT

from utils.error_handler import handle_interaction_error
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot import TicketBot


class BaseView(discord.ui.View):
    def __init__(self, bot: TicketBot, *, timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.log = get_logger(f"views.{type(self).__name__}")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        await handle_interaction_error(interaction, error, self.log)


class BaseModal(discord.ui.Modal):
    def __init__(self, bot: TicketBot, *, title: str) -> None:
        super().__init__(title=title)
        self.bot = bot
        self.log = get_logger(f"views.{type(self).__name__}")

    async def on_error(self, interaction: Interaction[ClientT], error: Exception, /) -> None:
        await handle_interaction_error(interaction, error, self.log)
