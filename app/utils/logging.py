"""Logging setup shared by the CLI, the web server and the pipeline."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOGGER_NAME = "tdip"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> logging.Logger:
    """Attach stderr (and optionally file) handlers to the application logger once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    if not any(getattr(h, "_tdip", False) for h in logger.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FORMAT))
        stream._tdip = True  # type: ignore[attr-defined]
        logger.addHandler(stream)
        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(_FORMAT))
            file_handler._tdip = True  # type: ignore[attr-defined]
            logger.addHandler(file_handler)
    return logger
