import pytest

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import ProcessingCancelledError


def test_cancellation_token_is_thread_safe_and_idempotent() -> None:
    cancellation = CancellationToken()

    assert not cancellation.is_cancelled
    check_cancelled(cancellation)

    cancellation.cancel()
    cancellation.cancel()

    assert cancellation.is_cancelled
    with pytest.raises(ProcessingCancelledError, match="canceló"):
        cancellation.check()


def test_an_absent_token_never_cancels() -> None:
    check_cancelled(None)
