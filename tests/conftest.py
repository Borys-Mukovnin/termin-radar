from __future__ import annotations

import pytest

from termin_alarm.config import Target


@pytest.fixture
def target() -> Target:
    return Target(
        key="demo",
        name="Demo Service",
        portal_id="qtermin-demo",
        service_id=12345,
        account_id="999",
        duration=20,
        app_future=10,
    )
