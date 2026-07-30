"""Small Windows lifecycle helpers used only while long local work is active."""

from __future__ import annotations

import ctypes
import logging
import os

LOGGER = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class SystemSleepBlocker:
    """Prevent automatic system sleep while a foreground processing job is active."""

    def __init__(self) -> None:
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        if self._active:
            return True
        if os.name != "nt":
            return False
        try:
            result = ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
            )
        except (AttributeError, OSError):
            result = 0
        if not result:
            LOGGER.warning("system_sleep_block_failed")
            return False
        self._active = True
        return True

    def stop(self) -> None:
        if not self._active:
            return
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except (AttributeError, OSError):
            LOGGER.warning("system_sleep_restore_failed")
        self._active = False
