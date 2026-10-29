<p align="center">
  <img src="assets/banner.png" alt="Ticket Bot banner" width="100%">
</p>

<h1 align="center">
  <img src="assets/profile-icon.png" alt="" width="28" height="28" style="vertical-align: middle; margin-right: 8px;">
  Ticket Bot
</h1>

<p align="center">
  <em>A production-grade Discord ticket bot. Slash commands only, TinyDB storage, built to run unattended on a Raspberry Pi under systemd.</em>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img alt="Python 3.10 or newer" src="https://img.shields.io/badge/python-3.10%2B-blue.svg"></a>
  <a href="https://github.com/Rapptz/discord.py"><img alt="discord.py 2.7" src="https://img.shields.io/badge/discord.py-2.7-5865F2.svg"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0-or-later" src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg"></a>
  <a href="tests"><img alt="Tests: pytest" src="https://img.shields.io/badge/tests-pytest-0A9EDC.svg"></a>
  <a href="deploy/README.md"><img alt="Deploy: systemd" src="https://img.shields.io/badge/deploy-systemd-orange.svg"></a>
</p>

---

## Contents

- [Why Ticket Bot](#why-ticket-bot)
- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Commands](#commands)
- [Project layout](#project-layout)
- [Development](#development)
- [Testing](#testing)
- [Deployment](#deployment)
- [Privacy](#privacy)
- [License](#license)

---

## Why Ticket Bot

Most ticket bots either run on a hosted service, drag in a database server, or freeze on Discord's rate limits. This one is different.

- **One Python process.** No Postgres, no Redis, no webhook receiver. Backups are a tarball.
- **Crash safe by construction.** Every write goes through a temp file plus `os.replace`, with directory fsync on POSIX.
- **Persistent views.** Close, Claim, Rename, Reopen and Delete buttons keep working across restarts, even after a reboot.
- **Closed means read-only.** Everyone with access loses `send_messages`, `attach_files`, `add_reactions` and thread creation. Support reopens to restore write access.
- **Self-contained transcripts.** Each ticket becomes one HTML file. No external scripts, no CDN links, small images embedded as `data:` URLs, all user content escaped.

---

## Features

### Panels and wizard

- Five-step `/setup` wizard covering name, support roles, ticket category, transcript channel and panel channel
- Per-panel Create ticket button, persistent through restarts
- Multiple panels per guild with `/admin panels` and `/admin delete-panel`

### Ticket lifecycle

- Slash commands: `/add`, `/remove`, `/claim`, `/rename`, `/close`, `/open`, `/delete`
- Matching buttons on the welcome message and on the closed embed
- One open ticket per user by default, configurable 1 to 25, with a jump link to the existing ticket
- `revoke` on `/remove` blocks re-adding until someone with Manage Server overrides

### Rate-limit awareness

- Discord allows 2 channel name or topic edits per 10 minutes per channel
- The bot keeps a local sliding-window budget, so a rename it knows Discord would reject is skipped with a note instead of freezing the command for minutes
- Close always locks permissions first (a permission edit, which is not rate limited), then writes the transcript, then renames

### Transcripts

- One HTML file per ticket, rendered by `utils/transcript.py`
- Discord markdown subset converted to HTML with all user content escaped
- Images under 2 MiB each and 10 MiB total are inlined so they outlive Discord's CDN
- Content-Security-Policy header set, no scripts, no remote resources

### Operations

- Rotating logs at `logs/bot.log` and `logs/error.log` alongside systemd journal output
- Cog auto-discovery: drop a `.py` into `cogs/` and it loads
- Atomic TinyDB storage, one JSON file per table, async-safe locking around every operation
- Config validation fails fast at startup with a clear message

---

## Requirements

- Python 3.10 or newer
- A Discord bot token with the **Server Members** and **Message Content** privileged intents enabled

Dependencies are pinned to minor versions in `requirements.txt`: discord.py 2.7, TinyDB 4.9, pydantic 2.14, python-dotenv 1.2, plus PyNaCl and davey for voice support.

---

## Quick start

```bash
git clone https://github.com/<you>/ticket-bot.git
cd ticket-bot

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set DISCORD_TOKEN

python main.py
```

In the Discord Developer Portal, open your application and enable **Server Members Intent** and **Message Content Intent** under the Bot tab. The bot exits with `PrivilegedIntentsRequired` if either is missing.

On first start the bot syncs its slash commands to the guild in `config.json` (`dev_guild_id`). Run `/setup` in your server to create your first panel.

---

## Configuration

### `.env`

| Variable          | Default         | Meaning                                                |
| ----------------- | --------------- | ------------------------------------------------------ |
| `DISCORD_TOKEN`   | required        | Bot token. Never commit it.                            |
| `LOG_LEVEL`       | `INFO`          | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`      |
| `DB_PATH`         | `./db`          | Folder for the TinyDB files                            |
| `TRANSCRIPT_PATH` | `./transcripts` | Folder for HTML transcripts                            |
| `LOG_PATH`        | `./logs`        | Folder for log files                                   |

### `config.json`

Unknown keys and missing keys both fail fast at startup.

```json
{
  "bot_name": "Ticket Bot",
  "dev_guild_id": 1562832771703902260,
  "bot_owner_ids": [877760893356744744],
  "activity": {
    "type": "playing",
    "name": "Ticket Bot"
  },
  "embed_theme": {
    "primary": "5865F2",
    "success": "57F287",
    "error": "ED4245",
    "warning": "FEE75C"
  },
  "ticket_format": {
    "open": "ticket-{number:04d}",
    "closed": "closed-{number:04d}"
  },
  "max_open_tickets_per_user": 1
}
```

| Key                           | Meaning                                                  |
| ----------------------------- | -------------------------------------------------------- |
| `dev_guild_id`                | Guild id to sync slash commands to during development    |
| `bot_owner_ids`               | User ids allowed to use owner-only checks                |
| `activity`                    | Presence block: `type`, `name`, optional `status`, `url` |
| `embed_theme`                 | Hex colours for `primary`, `success`, `error`, `warning` |
| `ticket_format`               | Channel name templates, must use `{number}`              |
| `max_open_tickets_per_user`   | 1 to 25                                                  |
| `support_can_manage_messages` | Give support roles `Manage Messages` inside tickets      |

---

## Commands

| Command                                                                                                                               | Who              | Notes                                                   |
|---------------------------------------------------------------------------------------------------------------------------------------| ---------------- | ------------------------------------------------------- |
| `/setup`                                                                                                                              | Manage Server    | Five-step panel wizard                                  |
| `/add user? role? ticket?`                                                                                                            | Support          | At least one of user or role                            |
| `/remove user? role? ticket? revoke?`                                                                                                 | Support          | `revoke` blocks re-adding until Manage Server overrides |
| `/close reason?`                                                                                                                      | Owner or support | Locks, transcribes, renames, moves                      |
| `/open`                                                                                                                               | Support          | Reopens a closed ticket                                 |
| `/delete`                                                                                                                             | Support          | Type `DELETE` to confirm                                |
| `/rename name?`                                                                                                                       | Support          | Empty name restores `ticket-XXXX`                       |
| `/claim user? reason?`                                                                                                                | Support          | Toggles the claim                                       |
| `/admin config` `/admin add-support-role` `/admin remove-support-role` `/admin closed-category` `/admin panels` `/admin delete-panel` | Manage Server    | Server configuration                                    |
| `/ping` `/help` `/id` `/permissionlevel`                                                                                              | Everyone         | Ephemeral replies                                       |
| `/debug`                                                                                                                              | Manage Server    | Version, host, uptime, latency, cog load status         |

To gate a new command, add `@is_support()` (or `is_admin`, `is_bot_owner`, `in_ticket`, `is_ticket_owner_or_support`) below `@app_commands.command(...)`. See `utils/checks.py`.

---

## Project layout

```text
bot.py  main.py  config.json  requirements.txt  pytest.ini
cogs/        base_command.py (BaseCog)  tickets.py  panels.py  admin.py  general.py
utils/       config  logger  database  checks  embeds  exceptions  error_handler
             ticket_service  transcript  rate_limit  views/
db/ logs/ transcripts/  runtime data (git-ignored)
assets/      banner.png  profile-icon.png
deploy/      ticket-bot.service  README.md
tests/       pytest suite
```

---

## Development

```bash
pip install -r requirements.txt
pytest
```

Command reference for contributors:

- **`BaseCog`** in `cogs/base_command.py` gives every cog a `self.db`, a namespaced `self.log`, and a `resolve_ticket(interaction, channel=None)` helper.
- **`TicketService`** in `utils/ticket_service.py` owns ticket state transitions. Cogs should not mutate ticket rows directly.
- **`utils/views/`** holds every persistent view. Any view that survives a restart must be registered in `TicketBot._register_persistent_views`.

---

## Testing

The suite checks behaviour, not just that code runs. Highlights:

- Atomic-write failure injection: a power cut between temp write and rename leaves the previous file intact
- Concurrent ticket numbering under `asyncio.gather`
- Per-user ticket limits, self-healing when a channel is manually deleted
- Lock-then-transcript-then-rename ordering in `/close`, plus stalled-rename timeout
- Rate-limit budget exhaustion is skipped locally instead of freezing
- Permission checks for every command decorator
- HTML escaping against script injection in transcripts
- The wizard, persistent view registration, and loading of every real cog against discord.py

```bash
pytest                    # full run
pytest -m "not slow"      # skip slow tests
pytest --cov=utils --cov=cogs
```

---

## Deployment

See [`deploy/README.md`](deploy/README.md) for a hardened Raspberry Pi walkthrough covering the systemd unit, filesystem permissions, encrypted backups, log rotation, updates and rollback, and an incident response runbook for a leaked token.

---

## Privacy

Transcripts contain private conversations between users and support staff. Store `transcripts/` on encrypted media, restrict the folder to `0700`, and include it in any backup as encrypted data. Treat logs the same way: they contain user ids and occasionally truncated message text.

---

## License

GNU General Public License v3.0 or later. See [`LICENSE`](LICENSE).