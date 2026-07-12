"""Scheduling: run the pipeline on a cron expression via a small daemon.

``muster schedule "*/15 * * * *"`` stores the expression beside muster.yaml;
``muster daemon start`` launches a detached loop that fires ``muster run``
on that schedule, with a PID file and a size-rotated log under runs/. The
daemon is deliberately small: it parses standard five-field cron syntax
itself (no extra dependency), runs the pipeline as a subprocess so a crash
in a run can never take the daemon down, records non-zero exits, and can
notify a webhook (URL from the MUSTER_WEBHOOK_URL environment variable —
never from configuration) when a run fails.

Prefer the operating system's own scheduler where one is available:
``muster schedule --print`` emits a ready-to-use systemd unit and cron line.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import subprocess  # nosec B404
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEDULE_FILE = "muster.schedule"
PID_FILE = "daemon.pid"
LOG_FILE = "daemon.log"
WEBHOOK_ENV = "MUSTER_WEBHOOK_URL"
_WEBHOOK_TIMEOUT = 10

_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

_FIELD_BOUNDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),  # 0 and 7 are both Sunday
)


class SchedulerError(RuntimeError):
    """Raised for invalid cron expressions or daemon control failures."""


def _parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise SchedulerError(f"invalid step in {name} field: {step_text!r}")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            if not (start_text.isdigit() and end_text.isdigit()):
                raise SchedulerError(f"invalid range in {name} field: {part!r}")
            start, end = int(start_text), int(end_text)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise SchedulerError(f"invalid value in {name} field: {part!r}")
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise SchedulerError(
                f"{name} field value out of range {low}-{high}: {part!r}"
            )
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronExpression:
    """A parsed five-field cron expression: minute-level scheduling."""

    raw: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, text: str) -> CronExpression:
        raw = text.strip()
        expanded = _ALIASES.get(raw, raw)
        fields = expanded.split()
        if len(fields) != 5:
            raise SchedulerError(
                f"a cron expression needs 5 fields (minute hour day month "
                f"weekday), got {len(fields)}: {raw!r}"
            )
        parsed = [
            _parse_field(field, name, low, high)
            for field, (name, low, high) in zip(fields, _FIELD_BOUNDS, strict=True)
        ]
        weekdays = frozenset(0 if v == 7 else v for v in parsed[4])
        return cls(
            raw=raw,
            minutes=parsed[0],
            hours=parsed[1],
            days=parsed[2],
            months=parsed[3],
            weekdays=weekdays,
            day_restricted=fields[2] != "*",
            weekday_restricted=fields[4] != "*",
        )

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        day_ok = moment.day in self.days
        # cron counts Sunday as 0; Python's weekday() counts Monday as 0.
        weekday_ok = (moment.weekday() + 1) % 7 in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            # Standard cron: both restricted means either may match.
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def next_after(self, moment: datetime) -> datetime:
        candidate = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + timedelta(days=366 * 4 + 1)  # spans any leap-year date
        while candidate < limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise SchedulerError(f"cron expression never fires: {self.raw!r}")


def schedule_path(root: Path) -> Path:
    return root / SCHEDULE_FILE


def write_schedule(root: Path, expression: CronExpression) -> Path:
    path = schedule_path(root)
    path.write_text(expression.raw + "\n", encoding="utf-8")
    return path


def read_schedule(root: Path) -> CronExpression:
    path = schedule_path(root)
    if not path.is_file():
        raise SchedulerError(
            f"no schedule found at {path}; set one with muster schedule \"<cron>\""
        )
    return CronExpression.parse(path.read_text(encoding="utf-8"))


def _muster_command() -> list[str]:
    """How to invoke this same muster installation from another process."""
    return [sys.executable, "-m", "muster"]


def systemd_unit(root: Path) -> str:
    command = " ".join(_muster_command())
    return f"""\
[Unit]
Description=Muster scheduled consolidation for {root}
After=network.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={command} daemon run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def cron_line(root: Path, expression: CronExpression) -> str:
    command = " ".join(_muster_command())
    return (
        f"{expression.raw} cd {root} && {command} run "
        ">> runs/cron.log 2>&1"
    )


def notify_webhook(url: str, payload: dict[str, object]) -> None:
    """POST a JSON failure notification; a webhook failure is logged, not fatal."""
    if not url.startswith(("https://", "http://")):
        logger.warning("ignoring %s: webhook URL must be http(s)", WEBHOOK_ENV)
        return
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # The scheme is validated above; only http(s) URLs are ever opened.
        with urllib.request.urlopen(request, timeout=_WEBHOOK_TIMEOUT):  # nosec B310
            pass
    except Exception as exc:  # noqa: BLE001 — the run result matters more
        logger.warning("webhook notification failed: %s", exc)


def run_once(root: Path, config_path: Path) -> int:
    """Run the pipeline in a subprocess; return its exit code."""
    # Our own executable with a fixed argv; nothing user-controlled, no shell.
    completed = subprocess.run(  # nosec B603
        [*_muster_command(), "run", "--config", str(config_path)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        logger.error(
            "scheduled run exited %d\n%s",
            completed.returncode,
            (completed.stderr or completed.stdout).strip()[-2000:],
        )
        url = os.environ.get(WEBHOOK_ENV, "").strip()
        if url:
            notify_webhook(
                url,
                {
                    "event": "muster_run_failed",
                    "project": str(root),
                    "exit_code": completed.returncode,
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
            )
    else:
        logger.info("scheduled run completed cleanly")
    return completed.returncode


def daemon_loop(
    root: Path,
    config_path: Path,
    *,
    now: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    run: Callable[[Path, Path], int] | None = None,
    max_iterations: int | None = None,
) -> None:
    """Fire the pipeline on the schedule, once per matching minute.

    The schedule file is re-read on every tick so ``muster schedule`` takes
    effect without a restart. ``now``, ``sleep``, ``run`` and
    ``max_iterations`` exist for tests; the defaults run forever.
    """
    runner = run or run_once
    last_fired: datetime | None = None
    iterations = 0
    logger.info("daemon started root=%s schedule=%s", root, read_schedule(root).raw)
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        moment = now().replace(second=0, microsecond=0)
        try:
            expression = read_schedule(root)
        except SchedulerError as exc:
            logger.error("schedule unreadable, daemon idling: %s", exc)
            sleep(30)
            continue
        if expression.matches(moment) and last_fired != moment:
            last_fired = moment
            logger.info("firing scheduled run at %s", moment.isoformat(timespec="minutes"))
            runner(root, config_path)
        # Wake shortly after the next minute boundary.
        sleep(max(1.0, 61 - now().second))


def configure_daemon_logging(runs_dir: Path) -> None:
    """Log to runs/daemon.log, size-rotated, alongside stderr defaults."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        runs_dir / LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def pid_path(runs_dir: Path) -> Path:
    return runs_dir / PID_FILE


def daemon_pid(runs_dir: Path) -> int | None:
    """The running daemon's PID, or None when stopped or stale."""
    path = pid_path(runs_dir)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def start_daemon(root: Path, config_path: Path, runs_dir: Path) -> int:
    """Launch the daemon loop detached; write its PID file; return the PID."""
    read_schedule(root)  # fail here, loudly, rather than in the detached child
    existing = daemon_pid(runs_dir)
    if existing is not None:
        raise SchedulerError(f"daemon already running with PID {existing}")
    runs_dir.mkdir(parents=True, exist_ok=True)
    # Our own executable with a fixed argv; nothing user-controlled, no shell.
    process = subprocess.Popen(  # nosec B603
        [*_muster_command(), "daemon", "run", "--config", str(config_path)],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path(runs_dir).write_text(f"{process.pid}\n", encoding="utf-8")
    return process.pid


def stop_daemon(runs_dir: Path, timeout: float = 5.0) -> int:
    """Terminate the daemon and remove the PID file; return the stopped PID."""
    pid = daemon_pid(runs_dir)
    if pid is None:
        pid_path(runs_dir).unlink(missing_ok=True)  # clear any stale file
        raise SchedulerError("daemon is not running")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    pid_path(runs_dir).unlink(missing_ok=True)
    return pid
