"""
Console logging for the management service (workers/, services/, api/) --
mirrors bootstrap/modules/util.py's get_logger() so both halves of this
project log consistently, and so failures surface in the service's own
console/journal output (e.g. under `journalctl -u mission-management`),
not only in a mission's own queryable step log
(core/types.py:MissionStatus.steps).
"""
from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    """Get (or create, on first call) a console logger with a consistent timestamp/level/name/message format."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger("mission_management")
