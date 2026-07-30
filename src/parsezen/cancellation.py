"""Thread-safe cooperative cancellation shared by the Qt adapter and core."""

from __future__ import annotations

from threading import Event

from parsezen.errors import ProcessingCancelledError


class CancellationToken:
    """Carry one cancellation request across synchronous processing boundaries."""

    def __init__(self) -> None:
        self._event = Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """Request cancellation from any thread."""
        self._event.set()

    def check(self) -> None:
        """Stop at a safe boundary when cancellation has been requested."""
        if self.is_cancelled:
            raise ProcessingCancelledError("El procesamiento se canceló de forma segura.")


def check_cancelled(cancellation: CancellationToken | None) -> None:
    """Check an optional token without manufacturing one for non-UI callers."""
    if cancellation is not None:
        cancellation.check()
