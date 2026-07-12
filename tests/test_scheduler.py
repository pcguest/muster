"""The scheduler: cron parsing and matching, the loop, daemon control."""

from datetime import datetime, timedelta

import pytest

import muster.scheduler as scheduler_module
from muster.scheduler import (
    CronExpression,
    SchedulerError,
    cron_line,
    daemon_loop,
    read_schedule,
    run_once,
    systemd_unit,
    write_schedule,
)


def test_parse_covers_steps_ranges_and_lists():
    expression = CronExpression.parse("*/15 8-17 1,15 * 1-5")
    assert expression.minutes == frozenset({0, 15, 30, 45})
    assert expression.hours == frozenset(range(8, 18))
    assert expression.days == frozenset({1, 15})
    assert expression.months == frozenset(range(1, 13))
    assert expression.weekdays == frozenset({1, 2, 3, 4, 5})


def test_aliases_and_sunday_seven():
    assert CronExpression.parse("@daily").matches(datetime(2026, 7, 12, 0, 0))
    on_seven = CronExpression.parse("0 0 * * 7")
    assert on_seven.matches(datetime(2026, 7, 12, 0, 0))  # a Sunday


@pytest.mark.parametrize(
    "bad",
    ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *", "5-2 * * * *"],
)
def test_invalid_expressions_are_rejected(bad):
    with pytest.raises(SchedulerError):
        CronExpression.parse(bad)


def test_restricted_day_and_weekday_match_either():
    # Standard cron rule: when both fields are restricted, either matching
    # day-of-month OR day-of-week fires.
    expression = CronExpression.parse("0 0 13 * 5")
    assert expression.matches(datetime(2026, 7, 13, 0, 0))  # the 13th, a Monday
    assert expression.matches(datetime(2026, 7, 17, 0, 0))  # a Friday, the 17th
    assert not expression.matches(datetime(2026, 7, 14, 0, 0))


def test_next_after_walks_to_the_next_firing_minute():
    expression = CronExpression.parse("30 9 * * *")
    moment = datetime(2026, 7, 12, 9, 30)
    assert expression.next_after(moment) == datetime(2026, 7, 13, 9, 30)
    assert expression.next_after(datetime(2026, 7, 12, 9, 29)) == moment


def test_schedule_round_trips_through_the_file(tmp_path):
    write_schedule(tmp_path, CronExpression.parse("*/5 * * * *"))
    assert read_schedule(tmp_path).raw == "*/5 * * * *"
    with pytest.raises(SchedulerError, match="no schedule found"):
        read_schedule(tmp_path / "elsewhere")


def test_daemon_loop_fires_once_per_matching_minute(tmp_path):
    write_schedule(tmp_path, CronExpression.parse("* * * * *"))
    clock = {"now": datetime(2026, 7, 12, 9, 30, 5)}
    fired: list[datetime] = []

    def fake_now():
        return clock["now"]

    def fake_sleep(seconds):
        clock["now"] += timedelta(seconds=30)

    def fake_run(root, config_path):
        fired.append(fake_now())
        return 0

    daemon_loop(
        tmp_path,
        tmp_path / "muster.yaml",
        now=fake_now,
        sleep=fake_sleep,
        run=fake_run,
        max_iterations=6,
    )
    # Six ticks over three minutes fire exactly once per minute.
    assert len(fired) == 3


def test_failed_run_notifies_the_webhook(tmp_path, monkeypatch):
    notified = {}

    class FakeCompleted:
        returncode = 2
        stdout = ""
        stderr = "held rows"

    monkeypatch.setattr(
        scheduler_module.subprocess, "run", lambda *a, **k: FakeCompleted()
    )
    monkeypatch.setattr(
        scheduler_module,
        "notify_webhook",
        lambda url, payload: notified.update({"url": url, "payload": payload}),
    )
    monkeypatch.setenv("MUSTER_WEBHOOK_URL", "https://hooks.example/muster")

    assert run_once(tmp_path, tmp_path / "muster.yaml") == 2
    assert notified["url"] == "https://hooks.example/muster"
    assert notified["payload"]["event"] == "muster_run_failed"
    assert notified["payload"]["exit_code"] == 2

    # No webhook configured: failure is recorded, nothing is sent.
    notified.clear()
    monkeypatch.delenv("MUSTER_WEBHOOK_URL")
    assert run_once(tmp_path, tmp_path / "muster.yaml") == 2
    assert notified == {}


def test_printable_units_carry_the_schedule_and_root(tmp_path):
    expression = CronExpression.parse("*/10 * * * *")
    unit = systemd_unit(tmp_path)
    assert f"WorkingDirectory={tmp_path}" in unit
    assert "daemon run" in unit
    line = cron_line(tmp_path, expression)
    assert line.startswith("*/10 * * * * ")
    assert str(tmp_path) in line


def test_daemon_start_status_stop_smoke(tmp_path):
    from muster.scheduler import daemon_pid, start_daemon, stop_daemon

    write_schedule(tmp_path, CronExpression.parse("0 0 29 2 *"))  # fires rarely
    runs_dir = tmp_path / "runs"
    pid = start_daemon(tmp_path, tmp_path / "muster.yaml", runs_dir)
    try:
        assert daemon_pid(runs_dir) == pid
        with pytest.raises(SchedulerError, match="already running"):
            start_daemon(tmp_path, tmp_path / "muster.yaml", runs_dir)
    finally:
        assert stop_daemon(runs_dir) == pid
    assert daemon_pid(runs_dir) is None
    with pytest.raises(SchedulerError, match="not running"):
        stop_daemon(runs_dir)
