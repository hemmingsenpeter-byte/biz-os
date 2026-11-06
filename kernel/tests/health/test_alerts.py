from datetime import UTC, datetime

from biz_kernel.health import open_alerts, raise_alert

T0 = datetime(2026, 3, 1, tzinfo=UTC)


def test_dedupe_and_acknowledge(session):
    a = raise_alert(session, "k", "s", "m", T0)
    assert raise_alert(session, "k", "s", "m", T0) is None
    assert open_alerts(session) == [a]
    a.acknowledged = True
    assert open_alerts(session) == []
