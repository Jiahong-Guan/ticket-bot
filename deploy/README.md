# Deploying on a Raspberry Pi (systemd)

Tested on Raspberry Pi OS Bookworm (Python 3.11). Paths assume a dedicated
service user and project directory; adjust if yours differ.

This version is written security-first. If you only want the minimal working
deploy, see `docs/deploy-quickstart.md`. This file calls out why each step
matters.

---

## 0. Threat model: what we are protecting

| Asset              | Where it lives                      | Consequence if leaked                                                             |
|--------------------|-------------------------------------|-----------------------------------------------------------------------------------|
| Bot token          | `.env`                              | Full bot takeover: read every ticket, impersonate the bot, exfiltrate transcripts |
| Ticket transcripts | `transcripts/<guild>/<n>-<ts>.html` | Private conversations between users and support staff                             |
| Database           | `db/*.json`                         | Ticket metadata, participant IDs, close reasons                                   |
| Logs               | journald + `logs/`                  | User IDs, channel IDs, occasional message fragments                               |
| Bot code           | `/opt/ticket-bot`                   | If writable by the bot, an exploit can persist across restarts                    |

Threats considered: stolen token, stolen disk or backup, accidental
world-readable file, compromised host process, insider with SSH to the Pi.
Not in scope: Discord-side compromise, supply-chain attacks on `discord.py`.

---

## 1. Discord Developer Portal (one time)

Bot tab, **Privileged Gateway Intents**: enable **Server Members Intent** and
**Message Content Intent**. Without them the bot exits with
`PrivilegedIntentsRequired`.

Invite with only these scopes and permissions (least privilege):

- Scopes: `bot`, `applications.commands`
- Permissions: View Channels, Manage Channels, Send Messages, Embed Links,
  Attach Files, Read Message History, Manage Messages.
- Do not grant Administrator, Manage Roles (server-level), or Manage
  Webhooks. The bot sets per-channel permission overwrites itself. That only
  requires Manage Channels plus the role hierarchy to allow it.

Rotate the token whenever a device with Developer Portal access is lost.
**Reset Token** invalidates the old one immediately; there is no propagation
delay.

---

## 2. Dedicated service user

Never run the bot as `pi`. A bot compromise would otherwise inherit personal
access to `~/.ssh`, shell history, and any file under `/home/pi`, even though
`pi` may not have sudo.

```bash
sudo useradd --system --home /opt/ticket-bot --shell /usr/sbin/nologin ticketbot
sudo install -d -o ticketbot -g ticketbot -m 0750 /opt/ticket-bot
sudo install -d -o ticketbot -g ticketbot -m 0700 \
    /opt/ticket-bot/db /opt/ticket-bot/transcripts /opt/ticket-bot/logs
```

You (the admin) still do work as `pi` or any sudo-capable user. Anything that
needs to run as the bot, such as updates or debugging, is
`sudo -u ticketbot ...`.

---

## 3. Filesystem layout and permissions

```text
/opt/ticket-bot/                 ticketbot:ticketbot  0750
├── .env                         ticketbot:ticketbot  0600   (bot token)
├── config.json                  ticketbot:ticketbot  0600   (owner IDs, channel IDs)
├── requirements.txt             ticketbot:ticketbot  0644
├── main.py, utils/, cogs/       ticketbot:ticketbot  0644/0755
├── venv/                        ticketbot:ticketbot  0755
├── db/                          ticketbot:ticketbot  0700   (db/*.json)
├── transcripts/                 ticketbot:ticketbot  0700   (per-guild HTML)
└── logs/                        ticketbot:ticketbot  0700   (bot.log, error.log)
```

Verify after install:

```bash
sudo find /opt/ticket-bot -type f -perm -o=r -ls
sudo stat -c '%a %U:%G %n' /opt/ticket-bot/.env /opt/ticket-bot/config.json
```

Both files must be `600 ticketbot:ticketbot`. If they are not, re-run the
`chmod`.

---

## 4. Install

All commands as an admin user (`pi`), not as `ticketbot`:

```bash
# clone / rsync code into a staging dir you own
sudo rsync -a --exclude .git --exclude venv --exclude db --exclude transcripts \
    --exclude logs --exclude .env \
    ./ /opt/ticket-bot/
sudo chown -R ticketbot:ticketbot /opt/ticket-bot

# create the venv as the service user
sudo -u ticketbot python3 -m venv /opt/ticket-bot/venv
sudo -u ticketbot /opt/ticket-bot/venv/bin/pip install --upgrade pip
sudo -u ticketbot /opt/ticket-bot/venv/bin/pip install -r /opt/ticket-bot/requirements.txt

# secrets: create as root, hand to service user, lock down
sudo -u ticketbot cp /opt/ticket-bot/.env.example /opt/ticket-bot/.env
sudo -u ticketbot nano /opt/ticket-bot/.env         # paste DISCORD_TOKEN
sudo -u ticketbot chmod 600 /opt/ticket-bot/.env
sudo -u ticketbot chmod 600 /opt/ticket-bot/config.json
```

**Never** commit `.env` or `config.json` with real values. Add both to
`.gitignore` if they are not already there.

---

## 5. Secrets handling

- **One secret in this repo**: `DISCORD_TOKEN`. Treat it like an SSH private
  key.
- **Token rotation**: Developer Portal, Bot, **Reset Token**. Do this
  quarterly, and immediately after any suspected exposure. Update `.env`,
  then `systemctl restart ticket-bot`.
- **`.env` never leaves the host.** Not in backups in plaintext (see §9), not
  in shell history (type the token into `nano`, do not
  `echo TOKEN=... >> .env`), not in `journalctl` (the bot must not log its
  token; verify on first run).
- **Do not bake the token into the systemd unit** via `Environment=`. It
  becomes visible to any local user via
  `systemctl show ticket-bot -p Environment`. Use `EnvironmentFile=` instead;
  systemd reads it as root and the file is 0600.
- **`config.json`** contains channel and role IDs, not secrets, but `0600`
  keeps the invite targets out of casual view. It also prevents the bot from
  being tricked into reading a config file an attacker replaced.

---

## 6. systemd unit (hardened)

`/etc/systemd/system/ticket-bot.service`:

```ini
[Unit]
Description=Discord Ticket Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ticketbot
Group=ticketbot
WorkingDirectory=/opt/ticket-bot
EnvironmentFile=/opt/ticket-bot/.env
ExecStart=/opt/ticket-bot/venv/bin/python -u main.py

# Restart policy
Restart=always
RestartSec=10

# Logs
StandardOutput=journal
StandardError=journal

# --- Sandbox ---
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
RestrictNamespaces=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM

# --- Writable paths (with ProtectSystem=strict, everything else is read-only) ---
ReadWritePaths=/opt/ticket-bot/db /opt/ticket-bot/transcripts /opt/ticket-bot/logs

# Default file mode for anything the bot creates: 0600 / 0700
UMask=0077

[Install]
WantedBy=multi-user.target
```

Why each hardening directive matters, briefly:

| Directive                          | Effect                                                                  |
|------------------------------------|-------------------------------------------------------------------------|
| `User=ticketbot`                   | Bot compromise does not equal your user's shell                         |
| `NoNewPrivileges`                  | Blocks setuid escalation from the bot process                           |
| `ProtectSystem=strict`             | `/usr`, `/etc`, `/opt` read-only, so the bot cannot modify its own code |
| `ProtectHome=true`                 | `/home/pi` invisible to the bot                                         |
| `PrivateTmp`                       | Isolated `/tmp`; no symlink races with other services                   |
| `ReadWritePaths`                   | Explicit allowlist for the only three dirs that mutate                  |
| `RestrictAddressFamilies`          | No `AF_NETLINK`, `AF_PACKET`, or `AF_BLUETOOTH`; no raw sockets         |
| `SystemCallFilter=@system-service` | syscall allowlist suitable for a network service                        |
| `UMask=0077`                       | Future files default to `600`, not `644`                                |

Run `systemd-analyze security ticket-bot.service` after deploying; aim for an
exposure score below 3.0 (0 is perfect, but Python needs a few syscalls
that keep it around 2.0 to 2.5).

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ticket-bot
sudo systemctl status ticket-bot
```

---

## 7. Firewall

The bot needs no inbound ports. All Discord traffic is outbound HTTPS/WSS
(443). If UFW is enabled, default-deny incoming:

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp   # adjust subnet
sudo ufw enable
```

Tighten SSH separately (in `/etc/ssh/sshd_config.d/99-hardening.conf`):

```text
PasswordAuthentication no
PermitRootLogin no
AllowUsers pi
```

Reload with `sudo systemctl reload ssh` after verifying a key-based login
still works in a second terminal.

---

## 8. Logs

Two log destinations exist by design:

- **journald**: systemd captures stdout and stderr of the process. Keeps
  local history and rotation for free.
- **`logs/bot.log` and `logs/error.log`**: written by the bot's own handler.
  Useful for `tail -f` while debugging; `error.log` is the fast path when
  something breaks.

Both contain user IDs and occasionally truncated message text. Treat `logs/`
as `0700` and do not ship them off-box without redaction. If you need
central logging, send only level >= WARNING.

Rotate the file logs so a slow leak cannot fill the SD card:

```bash
sudo tee /etc/logrotate.d/ticket-bot >/dev/null <<'EOF'
/opt/ticket-bot/logs/*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    su ticketbot ticketbot
    create 0600 ticketbot ticketbot
}
EOF
```

View:

```bash
journalctl -u ticket-bot -f
sudo tail -f /opt/ticket-bot/logs/error.log
```

If you see the token printed anywhere (in `systemctl status`, `journalctl`,
or `logs/bot.log`), rotate it immediately (see §11).

---

## 9. Backups (encrypted)

`db/*.json` and `transcripts/**` are the only state. Both contain personal
data. Backups therefore inherit the transcript threat model.

```bash
sudo systemctl stop ticket-bot
sudo tar -C /opt/ticket-bot -czf - db transcripts \
    | gpg --symmetric --cipher-algo AES256 -o ~/ticket-bot-$(date +%F).tgz.gpg
sudo systemctl start ticket-bot
```

- Store the gpg passphrase somewhere other than the Pi (password manager).
- Copy the archive off-box (USB, NAS, `rclone`). A backup on the same SD card
  that fails is not a backup.
- Restore:

  ```bash
  sudo systemctl stop ticket-bot
  sudo gpg -d ~/ticket-bot-2026-10-29.tgz.gpg \
      | sudo tar -C /opt/ticket-bot -xzf -
  sudo chown -R ticketbot:ticketbot /opt/ticket-bot/db /opt/ticket-bot/transcripts
  sudo systemctl start ticket-bot
  ```

- **Never back up `.env`** into the archive. Restore it manually from your
  password manager after a rebuild. Including it turns one stolen backup into
  a permanent bot takeover.
- Retention: keep 7 daily and 4 weekly. Transcripts are the largest growth
  vector; if you have retention concerns for privacy reasons (e.g. GDPR), add
  a periodic pruning job inside the bot rather than relying on backups
  expiring.

---

## 10. Updates

Pin versions in `requirements.txt` (`discord.py==2.4.0`, not `>=2.4`). For
stronger supply-chain guarantees:

```bash
sudo -u ticketbot /opt/ticket-bot/venv/bin/pip install pip-tools
sudo -u ticketbot /opt/ticket-bot/venv/bin/pip-compile --generate-hashes requirements.in
```

Deploy sequence:

```bash
sudo systemctl stop ticket-bot
sudo -u ticketbot bash -c '
  cd /opt/ticket-bot
  git pull --ff-only
  venv/bin/pip install --require-hashes -r requirements.txt
'
sudo systemctl start ticket-bot
```

Note: running `git pull` as `ticketbot` lets the bot user modify its own code.
That is fine for a hobby bot, but if you want stricter isolation, deploy from
a separate `deploy` user that owns `/opt/ticket-bot` and grant `ticketbot`
only read access plus write access on `db/`, `transcripts/`, and `logs/`.

Rollback:

```bash
sudo systemctl stop ticket-bot
sudo -u ticketbot git -C /opt/ticket-bot checkout <previous-tag>
sudo systemctl start ticket-bot
```

---

## 11. Incident response: token leaked

Suspicion triggers (in order of urgency):

1. Bot posts in a channel you do not recognise
2. `journalctl -u ticket-bot` shows commands you did not run
3. `.env` was in a `git status`, `git log`, or shared archive
4. A laptop that had the token was lost or compromised

Steps:

```bash
# 1. Kill the running instance
sudo systemctl stop ticket-bot

# 2. Rotate in the Discord Developer Portal, Bot, Reset Token

# 3. Update the secret on the Pi
sudo -u ticketbot nano /opt/ticket-bot/.env     # new token
sudo -u ticketbot chmod 600 /opt/ticket-bot/.env

# 4. Check for tampering before restarting
sudo -u ticketbot git -C /opt/ticket-bot status          # code unchanged?
sudo -u ticketbot git -C /opt/ticket-bot log --oneline -5
sudo find /opt/ticket-bot -newer /opt/ticket-bot/main.py -type f   # recent writes

# 5. Restart with a clean audit trail
sudo systemctl start ticket-bot
journalctl -u ticket-bot --since "1 hour ago" | grep -iE 'error|forbidden|unauth'
```

If the token was ever exposed on the internet (pastebin, CI logs, public
repo), assume it was scraped and rotate even if you see no abuse.
Automated scrapers watch GitHub's `/search` for `DISCORD_TOKEN=` on every
push.

---

## 12. Post-deploy checklist

Run through this once after setting up, and again after every upgrade:

```bash
# secrets
sudo stat -c '%a %U:%G %n' /opt/ticket-bot/.env /opt/ticket-bot/config.json
# expected: 600 ticketbot:ticketbot for both

# no world-readable files
sudo find /opt/ticket-bot -type f -perm -o=r -ls
# expected: empty

# systemd exposure score
systemd-analyze security ticket-bot.service | tail -3
# expected: "Overall exposure level ... N.N OK" with N.N < 3.0

# no privileged escalation possible
systemctl show ticket-bot -p NoNewPrivileges
# expected: NoNewPrivileges=yes

# token not visible in unit env
systemctl show ticket-bot -p Environment
# expected: empty (or only non-secret values from .env if you add any)

# no unexpected inbound
sudo ss -tlnp | grep -i python
# expected: empty

# bot connected
journalctl -u ticket-bot --since "2 minutes ago" | grep "Ready as"
# expected: one line with the bot's username
```

If any check fails, fix it before enabling the service with `enable --now`.
A working deploy that fails a security check is worse than a broken deployment
that passes.
