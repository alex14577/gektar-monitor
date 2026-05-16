"""Default log level resolution from environment."""

import logging
import os


def default_log_level() -> int:
    """Return default log level from FIS_LOG_LEVEL_DEFAULT env var (INFO if unset)."""
    name = os.environ.get("FIS_LOG_LEVEL_DEFAULT", "INFO").upper()
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        level = logging.INFO
    return level
