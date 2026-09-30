from pathlib import Path

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Pass an empty run only while the directory holds no test module."""
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and not any(
        Path(__file__).parent.glob("test_*.py")
    ):
        session.exitstatus = pytest.ExitCode.OK
