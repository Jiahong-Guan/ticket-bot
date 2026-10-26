from __future__ import annotations

import logging
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3
_ROOT_NAME = "ticketbot"


def setup_logging(level: str, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    main_file = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    error_file = RotatingFileHandler(
        log_dir / "error.log", maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    error_file.setLevel(logging.WARNING)
    stream = logging.StreamHandler(sys.stdout)

    for handler in (main_file, error_file, stream):
        handler.setFormatter(formatter)
        root.addHandler(handler)

    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def host_name() -> str:
    return socket.gethostname()
