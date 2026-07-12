"""HTTP transport for network targets: timeouts, retries, backoff, 429.

This is the availability leg of the CIA triad for publishing: every request
carries a timeout, transient failures (429, 5xx, network errors) are retried
with exponential backoff plus jitter, and a 429 ``Retry-After`` header is
honoured. Anything else fails fast with a redacted error. Tests patch
:func:`_open` and :func:`_sleep`; nothing here is called from the test suite
against a real network.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from typing import Any

from muster.credentials import redact_text
from muster.targets.base import TargetError

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 30.0
_MAX_RETRY_AFTER_SECONDS = 120.0

# Module-level indirection so tests can patch time and the network away.
_sleep = time.sleep


def _open(request: urllib.request.Request, timeout: int) -> Any:  # patched in tests
    # Every URL reaching here is constrained to http(s) by the target config
    # models (url/login_url validators, the instance_url https check).
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter: half fixed, half random."""
    base: float = min(0.5 * (2**attempt), _MAX_BACKOFF_SECONDS)
    # Jitter spreads retries out; it is not security material.
    return base / 2 + random.uniform(0, base / 2)  # nosec B311


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw is None:
        return None
    try:
        return min(float(raw), _MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return None


def request_json(
    url: str,
    *,
    method: str = "POST",
    headers: dict[str, str],
    data: bytes,
    timeout: int,
    max_retries: int,
    description: str,
) -> object:
    """Send one request and parse the JSON reply, retrying transient failures.

    Retries 429 (honouring Retry-After), 5xx and network errors up to
    ``max_retries`` times with exponential backoff and jitter. Any other
    HTTP status raises :class:`TargetError` immediately — a request the
    server has rejected outright will not become accepted by resending it.
    """
    attempt = 0
    while True:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _open(request, timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            status = exc.code
            transient = status == 429 or 500 <= status < 600
            if transient and attempt < max_retries:
                delay = _retry_after(exc) if status == 429 else None
                if delay is None:
                    delay = _backoff(attempt)
                attempt += 1
                logger.warning(
                    "%s got HTTP %d; retry %d of %d in %.1fs",
                    description,
                    status,
                    attempt,
                    max_retries,
                    delay,
                )
                _sleep(delay)
                continue
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # the status alone still tells the story
                detail = ""
            raise TargetError(
                f"{description} failed with HTTP {status}"
                + (f" after {attempt} retr{'y' if attempt == 1 else 'ies'}" if transient else "")
                + (f": {detail}" if detail else "")
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < max_retries:
                delay = _backoff(attempt)
                attempt += 1
                logger.warning(
                    "%s failed (%s); retry %d of %d in %.1fs",
                    description,
                    redact_text(str(exc)),
                    attempt,
                    max_retries,
                    delay,
                )
                _sleep(delay)
                continue
            raise TargetError(
                f"{description} failed after {attempt} retr{'y' if attempt == 1 else 'ies'}: {exc}"
            ) from None
        except json.JSONDecodeError as exc:
            raise TargetError(f"{description} returned a non-JSON reply: {exc}") from None
