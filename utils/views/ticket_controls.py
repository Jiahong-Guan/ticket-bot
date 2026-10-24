from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import discord

from utils import embeds
from utils.checks import has_support_access
from utils.database import Ticket, TicketStatus
from utils.exceptions import (
    PanelNotFound,
    PermissionDenied,
    TicketNotFound,
    TicketStateError,
)
from utils.views.base import BaseModal, BaseView

if TYPE_CHECKING:
    from bot import TicketBot

CLOSE_ID = "ticket_controls:close"
CLAIM_ID = "ticket_controls:claim"
RENAME_ID = "ticket_controls:rename"
REOPEN_ID = "ticket_closed:reopen"
DELETE_ID = "ticket_closed:delete"


def _bot(interaction: discord.Interaction) -> TicketBot:
    return cast("TicketBot", interaction.client)


def _member(interaction: discord.Interaction) -> discord.Member:
    if not isinstance(interaction.user, discord.Member):
        raise PermissionDenied("This can only be used in a server.")
    return interaction.user


async def load_ticket_context(
    interaction: discord.Interaction,
) -> tuple[Ticket, discord.TextChannel]:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        raise TicketNotFound()
    ticket = await _bot(interaction).db.get_ticket_by_channel(channel.id)
    if ticket is None:
        raise TicketNotFound()
    return ticket, channel


class TicketButtonView(BaseView):
    def __init__(self, bot: TicketBot, panel_id: int, label: str = "Create Ticket") -> None:
        super().__init__(bot, timeout=None)
        self.panel_id = panel_id
        button: discord.ui.Button[Any] = discord.ui.Button(
            label=label[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"create_ticket:{panel_id}",
            emoji="\U0001f3ab",
        )
        button.callback = self._on_click  # type: ignore[method-assign]
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        member = _member(interaction)
        guild = interaction.guild
        if guild is None:
            raise PermissionDenied("This can only be used in a server.")
        panel = await self.bot.db.get_panel(self.panel_id)
        if panel is None:
            raise PanelNotFound()
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, channel = await self.bot.tickets.create_ticket(guild, member, panel)
        await interaction.followup.send(
            embed=embeds.success(
                f"Your ticket is ready: {channel.mention}", title="Ticket created"
            ),
            ephemeral=True,
        )


class CloseTicketModal(BaseModal):
    reason: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, bot: TicketBot) -> None:
        super().__init__(bot, title="Close ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Close the ticket with the entered reason."""
        ticket, _ = await load_ticket_context(interaction)
        await interaction.response.defer()
        await self.bot.tickets.close_via_interaction(
            interaction, ticket, self.reason.value.strip() or None
        )


class RenameTicketModal(BaseModal):
    name: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="New channel name",
        placeholder="Leave empty to reset to the default name",
        required=False,
        max_length=100,
    )

    def __init__(self, bot: TicketBot) -> None:
        super().__init__(bot, title="Rename ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ticket, channel = await load_ticket_context(interaction)
        await interaction.response.defer()
        new_name = await self.bot.tickets.rename_ticket(
            channel, ticket, _member(interaction), self.name.value.strip() or None
        )
        await interaction.followup.send(
            embed=embeds.success(f"Ticket renamed to `{new_name}`.", title="Renamed")
        )


class DeleteTicketModal(BaseModal):
    confirm: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Type DELETE to confirm",
        placeholder="DELETE",
        required=True,
        max_length=10,
    )

    def __init__(self, bot: TicketBot) -> None:
        super().__init__(bot, title="Delete ticket")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.confirm.value.strip().upper() != "DELETE":
            await interaction.response.send_message(
                embed=embeds.warning("Confirmation text did not match. Nothing was deleted."),
                ephemeral=True,
            )
            return
        ticket, channel = await load_ticket_context(interaction)
        member = _member(interaction)
        if not await has_support_access(self.bot, member, ticket):
            raise PermissionDenied("Only support staff can delete tickets.")
        await interaction.response.send_message(
            embed=embeds.success(
                f"Deleting ticket #{ticket.number:04d}. This channel will disappear.",
                title="Deleting",
            ),
            ephemeral=True,
        )
        await self.bot.tickets.delete_ticket(channel, ticket, member)


class TicketControlView(BaseView):
    def __init__(self, bot: TicketBot) -> None:
        super().__init__(bot, timeout=None)

    @discord.ui.button(
        label="Close", style=discord.ButtonStyle.danger, custom_id=CLOSE_ID, emoji="\U0001f512"
    )
    async def close_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        ticket, _ = await load_ticket_context(interaction)
        member = _member(interaction)
        if member.id != ticket.owner_id and not await has_support_access(self.bot, member, ticket):
            raise PermissionDenied("Only the ticket owner or support can close this ticket.")
        if ticket.status != TicketStatus.OPEN:
            raise TicketStateError("This ticket is already closed.")
        await interaction.response.send_modal(CloseTicketModal(self.bot))

    @discord.ui.button(
        label="Claim", style=discord.ButtonStyle.success, custom_id=CLAIM_ID, emoji="\U0001f64b"
    )
    async def claim_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        ticket, channel = await load_ticket_context(interaction)
        await interaction.response.defer()
        updated = await self.bot.tickets.toggle_claim(channel, ticket, _member(interaction), None)
        text = (
            f"Claimed by <@{updated.claimed_by}>."
            if updated.claimed_by
            else "This ticket is no longer claimed."
        )
        await interaction.followup.send(embed=embeds.info(text, title="Claim updated"))

    @discord.ui.button(
        label="Rename",
        style=discord.ButtonStyle.secondary,
        custom_id=RENAME_ID,
        emoji="\u270f\ufe0f",
    )
    async def rename_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        ticket, _ = await load_ticket_context(interaction)
        if not await has_support_access(self.bot, _member(interaction), ticket):
            raise PermissionDenied("Only support staff can rename tickets.")
        await interaction.response.send_modal(RenameTicketModal(self.bot))


class ClosedControlView(BaseView):
    def __init__(self, bot: TicketBot) -> None:
        super().__init__(bot, timeout=None)

    @discord.ui.button(
        label="Reopen", style=discord.ButtonStyle.success, custom_id=REOPEN_ID, emoji="\U0001f513"
    )
    async def reopen_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        ticket, channel = await load_ticket_context(interaction)
        await interaction.response.defer()
        result = await self.bot.tickets.reopen_ticket(channel, ticket, _member(interaction))
        text = f"Ticket #{ticket.number:04d} was reopened. The owner can write again."
        if not result.renamed:
            text += "\nThe channel rename was skipped because of Discord's rate limit."
        await interaction.followup.send(embed=embeds.success(text, title="Reopened"))

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.danger,
        custom_id=DELETE_ID,
        emoji="\U0001f5d1\ufe0f",
    )
    async def delete_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        ticket, _ = await load_ticket_context(interaction)
        if not await has_support_access(self.bot, _member(interaction), ticket):
            raise PermissionDenied("Only support staff can delete tickets.")
        await interaction.response.send_modal(DeleteTicketModal(self.bot))
