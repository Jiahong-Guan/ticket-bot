from __future__ import annotations

import base64
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

import discord

from utils.logger import get_logger

log = get_logger("transcript")

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_INLINE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_INLINE_BYTES = 10 * 1024 * 1024
MAX_INLINE_FILES = 40
GROUP_WINDOW_SECONDS = 7 * 60
_EMBED_FALLBACK_COLOR = 0x202225


@dataclass(slots=True, frozen=True)
class TranscriptAttachment:
    filename: str
    url: str
    size: int = 0
    content_type: str | None = None
    data: bytes | None = None


@dataclass(slots=True, frozen=True)
class TranscriptEmbed:
    title: str | None = None
    description: str | None = None
    color: int | None = None
    footer: str | None = None
    fields: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True, frozen=True)
class TranscriptEntry:
    created_at: datetime
    author_id: int
    author_name: str
    content: str
    is_bot: bool = False
    edited_at: datetime | None = None
    attachments: tuple[TranscriptAttachment, ...] = ()
    embeds: tuple[TranscriptEmbed, ...] = ()
    buttons: tuple[str, ...] = ()


class AttachmentBudget:
    def __init__(
        self,
        max_each: int = MAX_INLINE_BYTES,
        max_total: int = MAX_TOTAL_INLINE_BYTES,
        max_files: int = MAX_INLINE_FILES,
    ) -> None:
        self.max_each = max_each
        self.remaining = max_total
        self.files_left = max_files

    def allows(self, size: int, content_type: str | None) -> bool:
        kind = (content_type or "").split(";")[0].strip().lower()
        return (
            kind in IMAGE_TYPES
            and 0 < size <= self.max_each
            and size <= self.remaining
            and self.files_left > 0
        )

    def consume(self, nbytes: int) -> None:
        self.remaining -= nbytes
        self.files_left -= 1


async def entry_from_message(
    message: discord.Message, budget: AttachmentBudget | None = None
) -> TranscriptEntry:
    budget = budget or AttachmentBudget()
    attachments: list[TranscriptAttachment] = []
    for att in message.attachments:
        data: bytes | None = None
        if budget.allows(att.size, att.content_type):
            try:
                data = await att.read()
            except discord.HTTPException:
                log.warning(f"Could not download attachment {att.filename}", exc_info=True)
            else:
                budget.consume(len(data))
        attachments.append(
            TranscriptAttachment(att.filename, att.url, att.size, att.content_type, data)
        )

    embeds = tuple(
        TranscriptEmbed(
            title=e.title,
            description=e.description,
            color=e.colour.value if e.colour else None,
            footer=e.footer.text,
            fields=tuple((f.name or "", f.value or "") for f in e.fields),
        )
        for e in message.embeds
    )
    buttons: list[str] = []
    for row in getattr(message, "components", None) or []:
        for child in getattr(row, "children", None) or []:
            label = getattr(child, "label", None)
            if label:
                buttons.append(str(label))

    return TranscriptEntry(
        created_at=message.created_at,
        author_id=message.author.id,
        author_name=message.author.display_name,
        content=str(message.clean_content or message.system_content),
        is_bot=bool(getattr(message.author, "bot", False)),
        edited_at=message.edited_at,
        attachments=tuple(attachments),
        embeds=embeds,
        buttons=tuple(buttons),
    )


_URL = re.compile(r"https?://(?:(?!&lt;|&gt;|&quot;)\S)+")
_TRAILING = ".,;:!?)"


def render_markdown(text: str) -> str:
    stash: list[str] = []

    def keep(markup: str) -> str:
        stash.append(markup)
        return f"\x00{len(stash) - 1}\x00"

    def link(match: re.Match[str]) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in _TRAILING:
            tail = url[-1] + tail
            url = url[:-1]
        return keep(f'<a href="{url}" rel="noopener noreferrer">{url}</a>') + tail

    out = escape(text.replace("\x00", ""))
    out = re.sub(
        r"```(?:[A-Za-z0-9_+-]*\n)?(.*?)```",
        lambda m: keep(f"<pre><code>{m.group(1)}</code></pre>"),
        out,
        flags=re.DOTALL,
    )
    out = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{m.group(1)}</code>"), out)
    out = _URL.sub(link, out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"__(.+?)__", r"<u>\1</u>", out)
    out = re.sub(r"~~(.+?)~~", r"<s>\1</s>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?![\w])", r"<em>\1</em>", out)
    out = out.replace("\n", "<br>")
    for index, markup in enumerate(stash):
        out = out.replace(f"\x00{index}\x00", markup)
    return out


_CSS = """
:root{color-scheme:dark light;--bg:#313338;--panel:#2b2d31;--text:#dbdee1;--muted:#949ba4;
--link:#00a8fc;--border:#1e1f22;--code:#1e1f22}
@media (prefers-color-scheme:light){:root{--bg:#ffffff;--panel:#f2f3f5;--text:#2e3338;
--muted:#5c6068;--link:#0068e0;--border:#d9dbde;--code:#e3e5e8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--link)}
header.info,section.users{background:var(--panel);border-bottom:1px solid var(--border);
padding:16px 20px}
header.info h1{margin:0 0 8px;font-size:20px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:0}
dt{color:var(--muted)}dd{margin:0;word-break:break-word}
section.users h2{margin:0 0 6px;font-size:14px;color:var(--muted);text-transform:uppercase}
section.users li{margin:2px 0}
main{padding:12px 0}
.msg{display:flex;gap:14px;padding:2px 20px}
.msg.first{margin-top:14px}
.msg:hover{background:rgba(127,127,127,.08)}
.avatar{flex:none;width:40px;height:40px;border-radius:50%;color:#fff;font-weight:600;
display:flex;align-items:center;justify-content:center}
.gutter{flex:none;width:40px;font-size:11px;color:var(--muted);text-align:right;opacity:0}
.msg:hover .gutter{opacity:1}
.body{min-width:0;flex:1}
.meta{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.name{font-weight:600}
.badge{background:#5865f2;color:#fff;border-radius:3px;font-size:10px;padding:1px 4px}
time,.edited{color:var(--muted);font-size:12px}
.content{word-break:break-word}
code{background:var(--code);border-radius:3px;padding:1px 4px;font-family:ui-monospace,monospace;
font-size:13px}
pre{background:var(--code);border-radius:4px;padding:8px;overflow-x:auto;margin:4px 0}
pre code{padding:0;background:none}
.att{margin-top:4px}
.att img{display:block;max-width:min(420px,100%);max-height:320px;border-radius:6px}
.embed{margin-top:6px;max-width:520px;background:var(--panel);border-left:4px solid #202225;
border-radius:4px;padding:8px 12px}
.embed .title{font-weight:600}
.embed .footer{margin-top:6px;font-size:12px;color:var(--muted)}
.field{margin-top:6px}.field b{display:block;font-size:13px}
.buttons{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}
.buttons span{background:#4e5058;color:#fff;border-radius:3px;padding:3px 12px;font-size:13px}
footer{padding:16px 20px;color:var(--muted);font-size:12px;border-top:1px solid var(--border)}
"""

_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _attachment_html(att: TranscriptAttachment) -> str:
    name = escape(att.filename)
    if att.data is not None:
        kind = (att.content_type or "").split(";")[0].strip().lower()
        encoded = base64.b64encode(att.data).decode("ascii")
        return f'<div class="att"><img alt="{name}" src="data:{kind};base64,{encoded}"></div>'
    size_kb = f" ({att.size / 1024:.1f} KB)" if att.size else ""
    if att.url.startswith("https://"):
        return (
            f'<div class="att"><a href="{escape(att.url)}" rel="noopener noreferrer">{name}</a>'
            f"{size_kb}</div>"
        )
    return f'<div class="att">{name}{size_kb}</div>'


def _embed_html(embed: TranscriptEmbed) -> str:
    color = embed.color if embed.color is not None else _EMBED_FALLBACK_COLOR
    parts = [f'<div class="embed" style="border-left-color:#{int(color) & 0xFFFFFF:06x}">']
    if embed.title:
        parts.append(f'<div class="title">{escape(embed.title)}</div>')
    if embed.description:
        parts.append(f'<div class="desc">{render_markdown(embed.description)}</div>')
    parts.extend(
        f'<div class="field"><b>{escape(name)}</b>{render_markdown(value)}</div>'
        for name, value in embed.fields
    )
    if embed.footer:
        parts.append(f'<div class="footer">{escape(embed.footer)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _message_html(entry: TranscriptEntry, *, continuation: bool) -> str:
    body: list[str] = []
    if entry.content:
        body.append(f'<div class="content">{render_markdown(entry.content)}</div>')
    body.extend(_attachment_html(a) for a in entry.attachments)
    body.extend(_embed_html(e) for e in entry.embeds)
    if entry.buttons:
        buttons = "".join(f"<span>{escape(label)}</span>" for label in entry.buttons)
        body.append(f'<div class="buttons">{buttons}</div>')
    inner = "".join(body)
    iso = escape(entry.created_at.astimezone(timezone.utc).isoformat())
    if continuation:
        short = entry.created_at.astimezone(timezone.utc).strftime("%H:%M")
        return (
            f'<div class="msg"><div class="gutter">{short}</div>'
            f'<div class="body">{inner}</div></div>'
        )
    initial = escape(entry.author_name[:1].upper() or "?")
    hue = entry.author_id % 360
    badge = '<span class="badge">BOT</span>' if entry.is_bot else ""
    edited = (
        f'<span class="edited">(edited {escape(_stamp(entry.edited_at))})</span>'
        if entry.edited_at
        else ""
    )
    return (
        '<div class="msg first">'
        f'<div class="avatar" style="background:hsl({hue} 55% 45%)">{initial}</div>'
        '<div class="body"><div class="meta">'
        f'<span class="name">{escape(entry.author_name)}</span>{badge}'
        f'<time datetime="{iso}">{escape(_stamp(entry.created_at))}</time>{edited}</div>'
        f"{inner}</div></div>"
    )


def render_transcript_html(
    *,
    guild_name: str,
    guild_id: int,
    channel_name: str,
    channel_id: int,
    ticket_number: int,
    owner_id: int,
    entries: list[TranscriptEntry],
    generated_at: datetime | None = None,
) -> str:
    generated = generated_at or datetime.now(timezone.utc)
    saved = sum(1 for e in entries for a in e.attachments if a.data is not None)
    linked = sum(1 for e in entries for a in e.attachments if a.data is None)
    names: dict[int, str] = {}
    counts: Counter[int] = Counter()
    for entry in entries:
        names[entry.author_id] = entry.author_name
        counts[entry.author_id] += 1

    rows: list[str] = []
    previous: TranscriptEntry | None = None
    for entry in entries:
        continuation = (
                previous is not None
                and previous.author_id == entry.author_id
                and not previous.embeds
                and 0 <= (entry.created_at - previous.created_at).total_seconds() < GROUP_WINDOW_SECONDS
        )
        rows.append(_message_html(entry, continuation=continuation))
        previous = entry

    users = "".join(
        f"<li>{count} - {escape(names[uid])} ({uid})</li>" for uid, count in counts.most_common()
    )
    title = f"Transcript of {channel_name}"
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        f'<header class="info"><h1>{escape(title)}</h1><dl>'
        f"<dt>Server</dt><dd>{escape(guild_name)} ({guild_id})</dd>"
        f"<dt>Channel</dt><dd>{escape(channel_name)} ({channel_id})</dd>"
        f"<dt>Ticket</dt><dd>#{ticket_number:04d}</dd>"
        f"<dt>Owner ID</dt><dd>{owner_id}</dd>"
        f"<dt>Messages</dt><dd>{len(entries)}</dd>"
        f"<dt>Attachments saved</dt><dd>{saved}</dd>"
        f"<dt>Attachments linked only</dt><dd>{linked}</dd>"
        "</dl></header>"
        f'<section class="users"><h2>Users</h2><ul>{users or "<li>none</li>"}</ul></section>'
        f"<main>{''.join(rows)}</main>"
        f"<footer>Generated {escape(_stamp(generated))}</footer></body></html>"
    )