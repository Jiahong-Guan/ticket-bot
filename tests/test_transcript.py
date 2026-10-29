from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from tests.fakes import make_message
from utils.transcript import (
    AttachmentBudget,
    TranscriptAttachment,
    TranscriptEmbed,
    TranscriptEntry,
    entry_from_message,
    render_markdown,
    render_transcript_html,
)

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def entry(content: str = "hi", *, author: int = 5, name: str = "Ann", minutes: float = 0, **kw):  # type: ignore[no-untyped-def]
    return TranscriptEntry(
        created_at=T0 + timedelta(minutes=minutes),
        author_id=author,
        author_name=name,
        content=content,
        **kw,
    )


def render(entries: list[TranscriptEntry], **kw: object) -> str:
    base = {
        "guild_name": "Logs",
        "guild_id": 1,
        "channel_name": "ticket-0001",
        "channel_id": 2,
        "ticket_number": 1,
        "owner_id": 5,
        "entries": entries,
        "generated_at": T0,
    }
    return render_transcript_html(**{**base, **kw})  # type: ignore[arg-type]


def test_page_has_no_scripts_and_no_remote_resources() -> None:
    page = render([entry("hello", attachments=(TranscriptAttachment("a.txt", "https://x/a.txt"),))])
    assert "<script" not in page.lower()
    assert "tickettool.xyz" not in page
    assert "Content-Security-Policy" in page
    assert "default-src 'none'" in page
    assert not re.search(r"<(?:link|iframe|object|embed)\b", page)
    # only data: images are ever auto-loaded
    assert not re.search(r'<img[^>]+src="(?!data:)', page)


def test_all_user_controlled_fields_are_escaped() -> None:
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>\"'"
    page = render(
        [
            entry(
                evil,
                name=evil,
                attachments=(TranscriptAttachment(evil, 'https://x/"onmouseover="alert(3)'),),
                embeds=(
                    TranscriptEmbed(
                        title=evil, description=evil, footer=evil, fields=((evil, evil),)
                    ),
                ),
                buttons=(evil,),
            )
        ],
        guild_name=evil,
        channel_name=evil,
    )
    assert "<script" not in page.lower()
    assert "<img src=x" not in page
    assert "onerror=alert(2)>" not in page.replace("&gt;", "")
    assert '"onmouseover="' not in page
    assert page.count("&lt;script&gt;") >= 6


def test_javascript_urls_are_never_linked() -> None:
    assert "href" not in render_markdown("javascript:alert(1) and data:text/html,x")
    assert 'href="https://ok.example/p"' in render_markdown("see https://ok.example/p now")


def test_nul_characters_cannot_forge_stash_placeholders() -> None:
    out = render_markdown("`<b>x</b>` \x000\x00")
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert out.count("<code>") == 1


def test_svg_and_non_image_attachments_are_never_embedded() -> None:
    page = render(
        [
            entry(
                attachments=(
                    TranscriptAttachment(
                        "x.svg", "https://x/x.svg", 10, "image/svg+xml", b"<svg/>"
                    ),
                )
            )
        ]
    )
    # Even if data were supplied, it only becomes a data: image; budget forbids svg upstream.
    assert AttachmentBudget().allows(10, "image/svg+xml") is False
    assert AttachmentBudget().allows(10, "text/html") is False
    assert page.count("<img") == 1


def test_markdown_subset() -> None:
    assert render_markdown("**b**") == "<strong>b</strong>"
    assert render_markdown("*i*") == "<em>i</em>"
    assert render_markdown("_i_") == "<em>i</em>"
    assert render_markdown("__u__") == "<u>u</u>"
    assert render_markdown("~~s~~") == "<s>s</s>"
    assert render_markdown("a\nb") == "a<br>b"
    assert render_markdown("snake_case_name") == "snake_case_name"
    assert render_markdown("2 * 3 * 4") == "2 * 3 * 4"


def test_code_is_not_formatted_inside() -> None:
    assert render_markdown("`**x**`") == "<code>**x**</code>"
    block = render_markdown("```py\nprint('**no**')\n```")
    assert "<pre><code>" in block
    assert "<strong>" not in block


def test_link_trailing_punctuation_is_excluded() -> None:
    out = render_markdown("go to https://example.com/a.")
    assert 'href="https://example.com/a"' in out
    assert out.endswith("</a>.")


def test_angle_bracket_wrapped_links() -> None:
    out = render_markdown("<https://example.com/a>")
    assert 'href="https://example.com/a"' in out
    assert "&gt;" in out


def test_header_user_counts_and_stats() -> None:
    page = render(
        [
            entry("a", author=5, name="Ann"),
            entry("b", author=5, name="Ann", minutes=1),
            entry("c", author=7, name="Bot", is_bot=True, minutes=2),
        ]
    )
    assert "<dt>Messages</dt><dd>3</dd>" in page
    assert "<li>2 - Ann (5)</li>" in page
    assert "<li>1 - Bot (7)</li>" in page
    assert "Logs (1)" in page
    assert "ticket-0001 (2)" in page
    assert "BOT</span>" in page
    assert "2026-10-24 12:00:00 UTC" in page


def test_consecutive_messages_are_grouped_until_gap_or_other_author() -> None:
    page = render(
        [
            entry("one"),
            entry("two", minutes=2),
            entry("three", minutes=30),
            entry("four", author=9, name="Bob", minutes=31),
        ]
    )
    assert page.count('class="msg first"') == 3  # one, three (gap), four (author)
    assert page.count('class="msg"') == 1  # two is folded under one


def test_embed_buttons_and_edit_marker_render() -> None:
    page = render(
        [
            entry(
                "",
                is_bot=True,
                embeds=(TranscriptEmbed("Title", "Body **x**", 0x1EC45C, "foot", (("F", "v"),)),),
                buttons=("Close", "Claim"),
                edited_at=T0 + timedelta(minutes=1),
            )
        ]
    )
    assert "border-left-color:#1ec45c" in page
    assert '<div class="title">Title</div>' in page
    assert "<strong>x</strong>" in page
    assert "<span>Close</span><span>Claim</span>" in page
    assert "(edited 2026-10-24 12:01:00 UTC)" in page


def test_empty_transcript_still_renders() -> None:
    page = render([])
    assert "<dt>Messages</dt><dd>0</dd>" in page
    assert "<li>none</li>" in page


def test_saved_images_are_inlined_and_counted_others_linked() -> None:
    page = render(
        [
            entry(
                attachments=(
                    TranscriptAttachment("p.png", "https://cdn/p.png", 24, "image/png", PNG),
                    TranscriptAttachment("d.zip", "https://cdn/d.zip", 2048),
                )
            )
        ]
    )
    assert f"data:image/png;base64,{base64.b64encode(PNG).decode()}" in page
    assert '<a href="https://cdn/d.zip"' in page
    assert "(2.0 KB)" in page
    assert "<dt>Attachments saved</dt><dd>1</dd>" in page
    assert "<dt>Attachments linked only</dt><dd>1</dd>" in page


def attachment(name: str, size: int, ctype: str, data: bytes | Exception = PNG) -> MagicMock:
    att = MagicMock()
    att.filename, att.url, att.size, att.content_type = name, f"https://cdn/{name}", size, ctype
    att.read = (
        AsyncMock(side_effect=data) if isinstance(data, Exception) else AsyncMock(return_value=data)
    )
    return att


@pytest.mark.asyncio
async def test_entry_downloads_small_images_only() -> None:
    msg = make_message("pic")
    msg.components = []
    msg.edited_at = None
    small = attachment("a.png", 24, "image/png")
    huge = attachment("b.png", 5 * 1024 * 1024, "image/png")
    text = attachment("c.txt", 10, "text/plain")
    msg.attachments = [small, huge, text]
    result = await entry_from_message(msg, AttachmentBudget())
    assert [a.data is not None for a in result.attachments] == [True, False, False]
    huge.read.assert_not_awaited()
    text.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_caps_total_files_and_bytes() -> None:
    budget = AttachmentBudget(max_each=100, max_total=150, max_files=5)
    msg = make_message("x")
    msg.components = []
    msg.edited_at = None
    msg.attachments = [attachment(f"{i}.png", 100, "image/png", b"a" * 100) for i in range(3)]
    result = await entry_from_message(msg, budget)
    assert [a.data is not None for a in result.attachments] == [True, False, False]

    capped = AttachmentBudget(max_files=1)
    msg.attachments = [attachment(f"{i}.png", 10, "image/png") for i in range(3)]
    result = await entry_from_message(msg, capped)
    assert sum(a.data is not None for a in result.attachments) == 1


@pytest.mark.asyncio
async def test_download_failure_falls_back_to_link() -> None:
    msg = make_message("x")
    msg.components = []
    msg.edited_at = None
    broken = attachment(
        "a.png",
        10,
        "image/png",
        discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone"),  # type: ignore[arg-type]
    )
    msg.attachments = [broken]
    result = await entry_from_message(msg, AttachmentBudget())
    assert result.attachments[0].data is None
    assert result.attachments[0].url == "https://cdn/a.png"


@pytest.mark.asyncio
async def test_entry_reads_embeds_buttons_bot_flag_and_system_text() -> None:
    msg = make_message("")
    msg.system_content = "user pinned a message"
    msg.author = SimpleNamespace(display_name="Ticket Bot", id=9, bot=True)
    msg.edited_at = T0
    msg.embeds = [
        SimpleNamespace(
            title="T",
            description="D",
            colour=discord.Colour(0x1EC45C),
            footer=SimpleNamespace(text="F"),
            fields=[SimpleNamespace(name="n", value="v")],
        )
    ]
    msg.components = [
        SimpleNamespace(children=[SimpleNamespace(label="Close"), SimpleNamespace(label=None)])
    ]
    result = await entry_from_message(msg)
    assert result.content == "user pinned a message"
    assert result.is_bot is True
    assert result.edited_at == T0
    assert result.embeds[0] == TranscriptEmbed("T", "D", 0x1EC45C, "F", (("n", "v"),))
    assert result.buttons == ("Close",)
