from __future__ import annotations

from pathlib import Path
from typing import Any

import discord
from discord import app_commands, Interaction
from discord._types import ClientT
from discord.app_commands import AppCommandError
from discord.ext import commands
from discord.ext.commands import Context, errors
from discord.ext.commands._types import BotT

from utils import VERSION, embeds
from utils.config import Settings
from utils.database import Database
from utils.error_handler import handle_interaction_error
from utils.logger import get_logger, host_name
from utils.ticket_service import TicketService
from utils.views.ticket_controls import ClosedControlView, TicketButtonView, TicketControlView

COGS_DIR = Path(__file__).resolve().parent / "cogs"
_NOT_COGS = {"base_command"}


class TicketTree(app_commands.CommandTree[Any]):
    async def on_error(self, interaction: Interaction[ClientT], error: AppCommandError, /) -> None:
        client = interaction.client
        if isinstance(client, TicketBot):
            await client.on_app_command_error(interaction, error)
        else:
            get_logger("tree").error("Command error", exc_info=error)


class TicketBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True  # privileged: resolve ticket participants
        intents.members = True  # privileged: needed so transcripts contain text
        super().__init__(
            command_prefix=commands.when_mentioned,  # type: ignore[arg-type]
            intents=intents,
            help_command=None,
            tree_cls=TicketTree,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.settings = settings
        self.db = Database(settings.db_path)
        self.tickets = TicketService(self)
        self.started_at = discord.utils.utcnow()
        self.synced_commands = 0
        self.loaded_cogs: list[str] = []
        self.failed_cogs: list[str] = []
        self.log = get_logger("bot")

    async def setup_hook(self) -> None:
        embeds.configure_theme(self.settings.embed_theme)
        await self.db.open()
        self.log.info(f"Starting ticket bot v{VERSION} on host {host_name()} "
                      f"| db={self.settings.db_path} "
                      f"| transcripts={self.settings.transcript_path}")
        if not self.settings.bot_owner_ids:
            self.log.warning("bot_owner_ids is empty in config.json; owner-only commands are off")

        await self._register_persistent_views()

        await self._load_cogs()

        guild = discord.Object(id=self.settings.dev_guild_id)

        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)

        except discord.HTTPException:
            self.log.exception("Application command sync failed")
        else:
            self.synced_commands = len(synced)
            self.log.info(f"Synced {self.synced_commands} application command(s)")

    async def _register_persistent_views(self):
        self.add_view(TicketControlView(self))
        self.add_view(ClosedControlView(self))
        count = 0
        for panel in await self.db.list_panels():
            if panel.message_id is None:
                continue
            self.add_view(
                TicketButtonView(self, panel.id, panel.button_label), message_id=panel.message_id
            )
            count += 1
        self.log.info(f"Registered persistent views ({count} panel button(s))")

    async def _load_cogs(self) -> None:
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.stem.startswith("_") or path.stem in _NOT_COGS:
                continue
            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
            except Exception:
                self.log.exception(f"Failed to load cog {extension}")
                self.failed_cogs.append(extension)
            else:
                self.loaded_cogs.append(extension)
                self.log.info(f"Loaded cog {extension}")
        self.log.info(f"Cog load finished: {len(self.loaded_cogs)} loaded, {len(self.failed_cogs)} failed")

    async def on_ready(self) -> None:
        if self.settings.activity:
            try:
                await self._set_presence()
            except (discord.HTTPException, TypeError):
                self.log.exception("Failed to set presence")

        self.log.info(
            f"Ready as {self.user} ({self.user.id if self.user else '?'}) "
            f"| guilds={len(self.guilds)} "
            f"| latency={self.latency * 1000:.0f}ms "
            f"| synced_commands={self.synced_commands}"
        )

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await handle_interaction_error(interaction, error, self.log)

    async def on_command_error(self, context: Context[BotT], exception: errors.CommandError, /) -> None:
        if isinstance(exception, commands.CommandNotFound):
            return
        self.log.error(f"Prefix command error in {context.command}", exc_info=exception)

    async def close(self) -> None:
        self.log.info("Shutting down")
        await super().close()
        await self.db.close()

    async def _set_presence(self) -> None:
        activity_cfg = self.settings.activity or {}
        activity_type_name = str(activity_cfg.get("type", "watching")).lower()
        activity_types = {
            "playing": discord.ActivityType.playing,
            "watching": discord.ActivityType.watching,
            "listening": discord.ActivityType.listening,
            "competing": discord.ActivityType.competing,
            "streaming": discord.ActivityType.streaming,
            "custom": discord.ActivityType.custom,
            "unknown": discord.ActivityType.unknown,
        }
        activity_type = activity_types.get(activity_type_name, discord.ActivityType.watching)

        kwargs: dict[str, Any] = {
            "type": activity_type,
            "name": activity_cfg.get("name", "over HackIT 2026"),
        }
        # streaming REQUIRES a url or the API rejects the presence
        if activity_type is discord.ActivityType.streaming:
            kwargs["url"] = activity_cfg.get("url", "https://twitch.tv/placeholder")
        activity = discord.Activity(**kwargs)

        status_name = str(activity_cfg.get("status", "online")).lower()
        status_map = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
            "offline": discord.Status.offline,
        }
        status = status_map.get(status_name, discord.Status.online)

        await self.change_presence(activity=activity, status=status)
        self.log.info(f"Presence set: {activity_type_name} {kwargs['name']} (status={status_name})")
