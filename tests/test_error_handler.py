from __future__ import annotations

import logging

import pytest

import discord
from discord import app_commands

from tests.fakes import make_interaction, make_member
from utils.error_handler import handle_interaction_error
from utils.exceptions import PermissionDenied, TicketLimitReached, TicketNotFound

log = logging.getLogger("test")


def sent_embed(interaction: object) -> discord.Embed:
    call = interaction.response.send_message.await_args  # type: ignore[attr-defined]
    return call.kwargs["embed"]  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_bot_error_shows_its_message_ephemerally() -> None:
    inter = make_interaction(user=make_member(1))
    await handle_interaction_error(inter, TicketNotFound(), log)
    embed = sent_embed(inter)
    assert "not a ticket channel" in (embed.description or "")
    assert inter.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_check_failure_message_is_shown() -> None:
    inter = make_interaction(user=make_member(1))
    await handle_interaction_error(inter, app_commands.CheckFailure("Nope, not you."), log)
    assert "Nope, not you." in (sent_embed(inter).description or "")


@pytest.mark.asyncio
async def test_wrapped_errors_are_unwrapped() -> None:
    inter = make_interaction(user=make_member(1))
    wrapped = app_commands.CommandInvokeError.__new__(app_commands.CommandInvokeError)
    wrapped.original = PermissionDenied("inner reason")
    await handle_interaction_error(inter, wrapped, log)
    assert "inner reason" in (sent_embed(inter).description or "")


@pytest.mark.asyncio
async def test_unexpected_error_hides_details_but_gives_reference(
    caplog: object,
) -> None:
    inter = make_interaction(user=make_member(1))
    await handle_interaction_error(inter, RuntimeError("secret internals"), log)
    text = sent_embed(inter).description or ""
    assert "secret internals" not in text
    assert "Reference:" in text


@pytest.mark.asyncio
async def test_ticket_limit_is_a_warning_with_link() -> None:
    inter = make_interaction(user=make_member(1))
    await handle_interaction_error(inter, TicketLimitReached("https://x/y", 1), log)
    embed = sent_embed(inter)
    assert "https://x/y" in (embed.description or "")
    assert "\u26a0" in (embed.title or "")


@pytest.mark.asyncio
async def test_uses_followup_when_already_responded() -> None:
    inter = make_interaction(user=make_member(1))
    inter.response.is_done.return_value = True
    await handle_interaction_error(inter, TicketNotFound(), log)
    inter.followup.send.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_is_idempotent_per_interaction() -> None:
    inter = make_interaction(user=make_member(1))
    await handle_interaction_error(inter, TicketNotFound(), log)
    await handle_interaction_error(inter, TicketNotFound(), log)
    assert inter.response.send_message.await_count == 1


@pytest.mark.asyncio
async def test_failure_to_reply_does_not_raise() -> None:
    inter = make_interaction(user=make_member(1))
    inter.response.send_message.side_effect = discord.HTTPException(
        response=type("R", (), {"status": 500, "reason": "x"})(),
        message="boom",  # type: ignore[arg-type]
    )
    await handle_interaction_error(inter, TicketNotFound(), log)  # must not raise
