from __future__ import annotations

import sys

import discord
from dotenv import load_dotenv

from bot import TicketBot
from utils.config import Settings
from utils.exceptions import ConfigurationError
from utils.logger import get_logger, setup_logging


def main() -> int:
    load_dotenv()
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc.user_message}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.log_path)
    log = get_logger("main")

    try:
        bot = TicketBot(settings)
        bot.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.exception("Discord rejected the token. Check DISCORD_TOKEN in .env")
        return 1
    except discord.PrivilegedIntentsRequired:
        log.exception(
            "Enable the Server Members and Message Content intents in the Discord Developer Portal"
        )
        return 1
    except Exception:
        log.exception("Fatal error; the bot is exiting")
        return 1
    log.info("Bot stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
