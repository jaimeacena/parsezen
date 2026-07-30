from types import SimpleNamespace

import parsezen.windows_integration as windows_module
from parsezen.windows_integration import SystemSleepBlocker


def test_sleep_blocker_balances_windows_execution_state(monkeypatch) -> None:
    calls: list[int] = []

    def set_execution_state(value: int) -> int:
        calls.append(value)
        return 1

    monkeypatch.setattr(windows_module.os, "name", "nt")
    monkeypatch.setattr(
        windows_module.ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(SetThreadExecutionState=set_execution_state)),
        raising=False,
    )
    blocker = SystemSleepBlocker()

    assert blocker.start()
    assert blocker.start()
    assert blocker.active
    blocker.stop()
    blocker.stop()

    assert calls == [
        windows_module._ES_CONTINUOUS | windows_module._ES_SYSTEM_REQUIRED,
        windows_module._ES_CONTINUOUS,
    ]
    assert not blocker.active
