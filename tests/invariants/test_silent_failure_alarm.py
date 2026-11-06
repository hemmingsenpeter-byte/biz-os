"""A schedule that misses its run raises an alert within one cycle."""

from datetime import UTC, datetime, timedelta

from biz_kernel.health import check_missed, open_alerts, register_schedule, report_run

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


class Collect:
    def __init__(self):
        self.sent = []

    def notify(self, alert):
        self.sent.append(alert)


def test_silent_failure_alarm(session):
    fake = Collect()
    register_schedule(session, "nightly", HOUR, T0)
    report_run(session, "nightly", True, T0)
    assert check_missed(session, T0 + timedelta(minutes=30), alerter=fake) == []
    assert check_missed(session, T0 + HOUR + timedelta(minutes=20), alerter=fake)  # within a cycle
    assert len(fake.sent) == 1
    # same missed cycle: no repeat
    assert check_missed(session, T0 + HOUR + timedelta(minutes=40), alerter=fake) == []
    # next missed cycle: new alert
    assert len(check_missed(session, T0 + 2 * HOUR + timedelta(minutes=20), alerter=fake)) == 1
    assert len(open_alerts(session)) == 2


def test_never_run_and_failed_run_alert(session):
    register_schedule(session, "never", HOUR, T0)
    register_schedule(session, "bad", HOUR, T0)
    now = T0 + timedelta(minutes=10)
    report_run(session, "bad", False, now)
    kinds = {a.kind for a in check_missed(session, now)}
    assert kinds == {"schedule.failed"}
    kinds = {a.kind for a in check_missed(session, T0 + HOUR + timedelta(minutes=10))}
    assert "schedule.missed" in kinds
