"""Logging configuration — console + rotating file."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(level: str = "INFO", file: str = "logs/rhbot.log") -> logging.Logger:
    os.makedirs(os.path.dirname(file) or ".", exist_ok=True)

    logger = logging.getLogger("rhbot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    fileh = RotatingFileHandler(file, maxBytes=5_000_000, backupCount=5)
    fileh.setFormatter(fmt)
    logger.addHandler(fileh)

    logger.propagate = False
    return logger
