from __future__ import annotations

import pytest

from utils import embeds
from utils.config import EmbedTheme
from utils.embeds import EmbedColor, configure_theme, resolve_color


@pytest.fixture(autouse=True)
def reset_theme() -> None:
    configure_theme(
        EmbedTheme(primary="5865F2", success="57F287", error="ED4245", warning="FEE75C")
    )


def test_default_palette() -> None:
    assert embeds.success("ok").colour.value == 0x57F287  # type: ignore[union-attr]
    assert embeds.error("no").colour.value == 0xED4245  # type: ignore[union-attr]
    assert embeds.warning("hm").colour.value == 0xFEE75C  # type: ignore[union-attr]
    assert embeds.info("fyi").colour.value == 0x5865F2  # type: ignore[union-attr]


def test_theme_override_changes_colours() -> None:
    configure_theme(
        EmbedTheme(primary="111111", success="222222", error="333333", warning="444444")
    )
    assert resolve_color(EmbedColor.ERROR) == 0x333333
    assert embeds.error("x").colour.value == 0x333333  # type: ignore[union-attr]
    assert embeds.panel_embed("p").colour.value == 0x111111  # type: ignore[union-attr]
    # Colours without an override keep their default.
    assert resolve_color(EmbedColor.CLOSED) == int(EmbedColor.CLOSED)


def test_titles_carry_status_marker() -> None:
    assert "Success" in (embeds.success("x").title or "")
    assert "Custom" in (embeds.error("x", title="Custom").title or "")


def test_panel_embed_matches_spec_text() -> None:
    embed = embeds.panel_embed("Billing")
    assert embed.title == "Support Tickets"
    assert embed.description == "To create a ticket use the Create ticket button"
    assert embed.footer.text == "Billing"


def test_ticket_created_shows_claim_state() -> None:
    unclaimed = embeds.ticket_created(owner_id=5, panel_name="P", number=7)
    claimed = embeds.ticket_created(owner_id=5, panel_name="P", number=7, claimed_by_id=9)
    assert unclaimed.title == "Ticket #0007"
    assert "<@5>" in (unclaimed.description or "")
    values = {f.name: f.value for f in claimed.fields}
    assert values["Claimed by"] == "<@9>"
    assert {f.name: f.value for f in unclaimed.fields}["Claimed by"] == "Unclaimed"


def test_ticket_closed_with_and_without_transcript() -> None:
    plain = embeds.ticket_closed(number=1, closed_by_id=2, reason=None)
    assert {f.name: f.value for f in plain.fields}["Reason"] == "No reason provided"
    assert "Transcript" not in {f.name for f in plain.fields}
    linked = embeds.ticket_closed(
        number=1, closed_by_id=2, reason="done", transcript_url="https://example.com/t"
    )
    assert "https://example.com/t" in {f.name: f.value for f in linked.fields}["Transcript"]


def test_transcript_embed_fields() -> None:
    embed = embeds.transcript(
        number=3, owner_id=1, closed_by_id=2, message_count=15, channel_name="closed-0003"
    )
    values = {f.name: f.value for f in embed.fields}
    assert values["Messages"] == "15"
    assert values["Closed by"] == "<@2>"
