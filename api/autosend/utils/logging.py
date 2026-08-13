import logging
import sys

from autosend.config import settings

_LEVEL = getattr(logging, settings.log_level.upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Single place all modules get a logger from, so level/format/handler
    stay consistent and configurable via LOG_LEVEL without touching every
    call site."""
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(_LEVEL)

    handler = logging.StreamHandler(sys.stdout)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False  # avoid double-logging via the root logger

    return logger
