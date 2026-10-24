from __future__ import annotations

import logging
import uuid

import discord
from discord import app_commands

from utils import embeds
from utils.exceptions import BotError, TicketLimitReached

_HANDLED_KEY = "error_handled"


async def _reply(interaction: discord.Interaction, embed: discord.Embed) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _unwrap(error: Exception) -> Exception:
    original = getattr(error, "original", None)
    return original if isinstance(original, Exception) else error


async def handle_interaction_error(
    interaction: discord.Interaction,
    error: Exception,
    logger: logging.Logger,
) -> None:
    if interaction.extras.get(_HANDLED_KEY):
        return
    interaction.extras[_HANDLED_KEY] = True

    cause = _unwrap(error)
    embed: discord.Embed

    if isinstance(cause, TicketLimitReached):
        logger.info("Ticket limit hit by user %s", interaction.user.id)
        embed = embeds.warning(cause.user_message, title=cause.title)
    elif isinstance(cause, BotError):
        logger.info("%s: %s (user %s)", type(cause).__name__, cause, interaction.user.id)
        embed = embeds.error(cause.user_message, title=cause.title)
    elif isinstance(cause, app_commands.CommandOnCooldown):
        embed = embeds.warning(f"Slow down. Try again in {cause.retry_after:.1f}s.")
    elif isinstance(cause, app_commands.CheckFailure):
        logger.info(f"Check failed for user {interaction.user.id}: {cause}")
        embed = embeds.error(str(cause) or "You cannot use this command.", title="Denied")
    elif isinstance(cause, discord.Forbidden):
        logger.exception("Missing Discord permissions")
        embed = embeds.error(
            "I am missing a permission needed to do that. "
            "Check my role and the channel or category permissions.",
            title="Missing permissions",
        )
    else:
        reference = uuid.uuid4().hex[:8]
        logger.exception(f"Unhandled interaction error [ref {reference}]", exc_info=cause)
        embed = embeds.error(
            "Something went wrong. Please try again or contact an admin.\n"
            f"Reference: `{reference}`",
            title="Unexpected error",
        )

    try:
        await _reply(interaction, embed)
    except discord.HTTPException:
        logger.exception(f"Could not deliver error embed to user {interaction.user.id}", )
