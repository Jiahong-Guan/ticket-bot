from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import discord

from utils import embeds
from utils.database import GuildSettings
from utils.exceptions import PermissionDenied
from utils.views.base import BaseModal, BaseView
from utils.views.ticket_controls import TicketButtonView

if TYPE_CHECKING:
    from bot import TicketBot

TOTAL_STEPS = 5
WIZARD_TIMEOUT_SECONDS = 600.0

STEP_TITLES: dict[int, str] = {
    1: "Name",
    2: "Support roles",
    3: "Ticket category",
    4: "Transcript channel",
    5: "Panel channel",
}
STEP_HELP: dict[int, str] = {
    1: "Choose a name for this panel. Members only see it in the panel footer.",
    2: "Select every role that should be able to see and answer tickets.",
    3: "Select the category where new ticket channels are created.",
    4: "Select the text channel that receives transcripts of closed tickets.",
    5: "Select the text channel where the Create Ticket panel is posted, then press Finish.",
}

@dataclass(slots=True)
class WizardState:
    owner_id: int
    guild_id: int
    step: int = 1
    name: str | None = None
    support_role_ids: list[int] = field(default_factory=list)
    category_id: int | None = None
    transcript_channel_id: int | None = None
    panel_channel_id: int | None = None


class PanelNameModal(BaseModal):
    name: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Panel name", placeholder="Support", min_length=1, max_length=100
    )

    def __init__(self, view: PanelWizardView) -> None:
        super().__init__(view.bot, title="Panel name")
        self.wizard = view
        if view.state.name:
            self.name.default = view.state.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.name.value.strip()
        if not value:
            await interaction.response.send_message(
                embed=embeds.warning("The panel name cannot be empty."),
                ephemeral=True
            )
            return
        self.wizard.state.name = value
        await self.wizard.refresh(interaction)


class PanelWizardView(BaseView):
    def __init__(self, bot: TicketBot, state: WizardState):
        super().__init__(bot, timeout=WIZARD_TIMEOUT_SECONDS)
        self.state = state
        self.origin: discord.Interaction | None = None
        self._render()

    def build_embed(self) -> discord.Embed:
        step = self.state.step
        embed = embeds.info(
            STEP_HELP[step], title=f"Panel setup: step {step}/{TOTAL_STEPS} - {STEP_TITLES[step]}"
        )
        embed.add_field(name="Current selection", value=self._selection_text(), inline=False)
        return embed

    def _selection_text(self) -> str:
        s = self.state
        return "\n".join(
            [
                f"**Name:** {s.name or 'not set'}",
                "**Support roles:** "
                + (", ".join(f"<@&{r}>" for r in s.support_role_ids) or "not set"),
                f"**Category:** {f'<#{s.category_id}>' if s.category_id else 'not set'}",
                "**Transcripts:** "
                + (f"<#{s.transcript_channel_id}>" if s.transcript_channel_id else "not set"),
                "**Panel channel:** "
                + (f"<#{s.panel_channel_id}>" if s.panel_channel_id else "not set"),
            ]
        )

    def _render(self) -> None:
        self.clear_items()
        step = self.state.step
        if step == 1:
            label = "Edit name" if self.state.name else "Set name"
            button: discord.ui.Button[Any] = discord.ui.Button(
                label=label, style=discord.ButtonStyle.primary, row=0
            )
            button.callback = self._open_name_modal  # type: ignore[method-assign]
            self.add_item(button)
        elif step == 2:
            self._add_role_select()
        elif step == 3:
            self._add_channel_select(
                [discord.ChannelType.category],
                "Pick a category",
                self.state.category_id,
                self._set_category,
            )
        elif step == 4:
            self._add_channel_select(
                [discord.ChannelType.text],
                "Pick a transcript channel",
                self.state.transcript_channel_id,
                self._set_transcript,
            )
        else:
            self._add_channel_select(
                [discord.ChannelType.text],
                "Pick the panel channel",
                self.state.panel_channel_id,
                self._set_panel_channel,
            )

        back: discord.ui.Button[Any] = discord.ui.Button(
            label="<- Back", style=discord.ButtonStyle.secondary, disabled=step == 1, row=1
        )
        back.callback = self._go_back  # type: ignore[method-assign]
        forward: discord.ui.Button[Any] = discord.ui.Button(
            label="Finish" if step == TOTAL_STEPS else "Save & Continue",
            style=discord.ButtonStyle.success,
            row=1,
        )
        forward.callback = self._go_forward  # type: ignore[method-assign]
        cancel: discord.ui.Button[Any] = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger, row=1
        )
        cancel.callback = self._cancel  # type: ignore[method-assign]
        for item in (back, forward, cancel):
            self.add_item(item)

    def _add_role_select(self) -> None:
        defaults = [
            discord.SelectDefaultValue(id=rid, type=discord.SelectDefaultValueType.role)
            for rid in self.state.support_role_ids
        ]
        select: discord.ui.RoleSelect[Any] = discord.ui.RoleSelect(
            placeholder="Pick one or more support roles",
            min_values=1,
            max_values=25,
            default_values=defaults,
            row=0,
        )

        async def on_select(interaction: discord.Interaction) -> None:
            self.state.support_role_ids = [role.id for role in select.values]
            await self.refresh(interaction)

        select.callback = on_select  # type: ignore[method-assign]
        self.add_item(select)

    def _add_channel_select(
            self,
            types: list[discord.ChannelType],
            placeholder: str,
            current: int | None,
            setter: Any,
    ) -> None:
        defaults = (
            [discord.SelectDefaultValue(id=current, type=discord.SelectDefaultValueType.channel)]
            if current
            else []
        )
        select: discord.ui.ChannelSelect[Any] = discord.ui.ChannelSelect(
            channel_types=types,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=0,
        )

        async def on_select(interaction: discord.Interaction) -> None:
            setter(select.values[0].id)
            await self.refresh(interaction)

        select.callback = on_select  # type: ignore[method-assign]
        self.add_item(select)

    def _set_category(self, channel_id: int) -> None:
        self.state.category_id = channel_id

    def _set_transcript(self, channel_id: int) -> None:
        self.state.transcript_channel_id = channel_id

    def _set_panel_channel(self, channel_id: int) -> None:
        self.state.panel_channel_id = channel_id

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._render()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.state.owner_id:
            return True
        await interaction.response.send_message(
            embed=embeds.error("Only the admin who ran /setup can use this wizard."),
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        if self.origin is None:
            return
        with contextlib.suppress(discord.HTTPException):
            await self.origin.edit_original_response(
                embed=embeds.warning("Setup timed out. Run /setup again.", title="Timed out"),
                view=None,
            )

    async def _open_name_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PanelNameModal(self))

    async def _go_back(self, interaction: discord.Interaction) -> None:
        self.state.step = max(1, self.state.step - 1)
        await self.refresh(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=embeds.info("Panel setup cancelled.", title="Cancelled"), view=None
        )
        self.stop()

    def _validate_step(self, guild: discord.Guild) -> str | None:
        s = self.state
        if s.step == 1 and not s.name:
            return "Set a panel name first."
        if s.step == 2:
            if not s.support_role_ids:
                return "Select at least one support role."
            for rid in s.support_role_ids:
                role = guild.get_role(rid)
                if role is None or role.is_default():
                    return "Support roles must be real roles (not @everyone)."
        if s.step == 3 and s.category_id is None:
            return "Select a ticket category."
        if s.step == 4 and s.transcript_channel_id is None:
            return "Select a transcript channel."
        if s.step == 5 and s.panel_channel_id is None:
            return "Select the panel channel."
        return None

    def _permission_problems(self, guild: discord.Guild) -> list[str]:
        s = self.state
        me = guild.me
        problems: list[str] = []
        checks: list[tuple[int | None, str, list[str]]] = [
            (s.category_id, "ticket category", ["view_channel", "manage_channels", "manage_roles"]),
            (
                s.transcript_channel_id,
                "transcript channel",
                ["view_channel", "send_messages", "attach_files", "embed_links"],
            ),
            (
                s.panel_channel_id,
                "panel channel",
                ["view_channel", "send_messages", "embed_links"],
            ),
        ]
        for channel_id, label, needed in checks:
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                problems.append(f"The {label} no longer exists.")
                continue
            perms = channel.permissions_for(me)
            missing = [p.replace("_", " ") for p in needed if not getattr(perms, p)]
            if missing:
                problems.append(
                    f"I need **{', '.join(missing)}** in the {label} ({channel.mention})."
                )
        return problems

    async def _go_forward(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            raise PermissionDenied("This can only be used in a server.")
        problem = self._validate_step(guild)
        if problem:
            await interaction.response.send_message(embed=embeds.warning(problem), ephemeral=True)
            return
        if self.state.step < TOTAL_STEPS:
            self.state.step += 1
            await self.refresh(interaction)
            return
        await self._finish(interaction, guild)

    async def _finish(self, interaction: discord.Interaction, guild: discord.Guild) -> None:
        problems = self._permission_problems(guild)
        if problems:
            await interaction.response.send_message(
                embed=embeds.warning("\n".join(problems), title="Missing bot permissions"),
                ephemeral=True,
            )
            return
        s = self.state
        if (
                s.name is None
                or s.category_id is None
                or s.transcript_channel_id is None
                or s.panel_channel_id is None
        ):
            raise PermissionDenied("The wizard is incomplete.")  # unreachable after validation
        await interaction.response.defer()

        panel = await self.bot.db.create_panel(
            guild_id=guild.id,
            name=s.name,
            support_role_ids=s.support_role_ids,
            category_id=s.category_id,
            transcript_channel_id=s.transcript_channel_id,
            panel_channel_id=s.panel_channel_id,
        )
        target = guild.get_channel(s.panel_channel_id)
        if not isinstance(target, discord.TextChannel):
            await self.bot.db.delete_panel(panel.id)
            raise PermissionDenied("The panel channel must be a text channel.")
        try:
            message = await target.send(
                embed=embeds.panel_embed(panel.name),
                view=TicketButtonView(self.bot, panel.id, panel.button_label),
            )
        except discord.HTTPException:
            await self.bot.db.delete_panel(panel.id)
            raise
        await self.bot.db.update_panel(replace(panel, message_id=message.id))

        def merge(current: GuildSettings) -> GuildSettings:
            return replace(
                current,
                support_roles=sorted({*current.support_roles, *s.support_role_ids}),
                ticket_category=s.category_id,
                transcript_channel=s.transcript_channel_id,
                panel_channel=s.panel_channel_id,
                panel_message_id=message.id,
            )

        await self.bot.db.modify_guild(guild.id, merge)
        self.log.info(f"Panel {panel.id} created in guild {guild.id} by {s.owner_id}")
        await interaction.edit_original_response(
            embed=embeds.success(
                f"Panel **{panel.name}** is live in {target.mention}.", title="Setup complete"
            ),
            view=None,
        )
        self.stop()
