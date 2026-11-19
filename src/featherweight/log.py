"""Logging setup. Libraries log through `logging.getLogger(__name__)`; entry points call `setup_logging`."""

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_HANDLER_FLAG = "_featherweight_handler"


def setup_logging(level: str | int | None = None) -> logging.Logger:
    """Send `featherweight.*` logs to stdout as plain text. Safe to call more than once.

    Level comes from the argument, else `FTW_LOG_LEVEL`, else INFO.
    """
    logger = logging.getLogger("featherweight")
    logger.setLevel(level or os.environ.get("FTW_LOG_LEVEL", "INFO"))
    if not any(getattr(h, _HANDLER_FLAG, False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(FORMAT))
        setattr(handler, _HANDLER_FLAG, True)
        logger.addHandler(handler)
    return logger
