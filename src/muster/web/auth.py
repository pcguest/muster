"""Authentication for the web interface: one token, one user, local first.

A random token is generated the first time ``muster serve`` runs and stored
in the OS keyring (service ``muster``, name ``MUSTER_WEB_TOKEN``); when no
keyring backend exists the token falls back to a ``.muster-token`` file
beside muster.yaml with owner-only permissions. Presenting the token at
/login opens a session: an HttpOnly, SameSite=Strict cookie holding a
random identifier for an in-memory, expiring session record. Every
mutating form carries a per-session CSRF token, and mutating routes are
rate-limited per client address. Nothing here trusts the network: the
server binds 127.0.0.1 unless explicitly told otherwise.
"""

from __future__ import annotations

import logging
import secrets
import stat
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from muster.credentials import KEYRING_SERVICE, register_secret

logger = logging.getLogger(__name__)

# These are the *names* of where the token lives, not the token itself.
TOKEN_KEYRING_NAME = "MUSTER_WEB_TOKEN"  # nosec B105
TOKEN_FILE = ".muster-token"  # nosec B105
SESSION_COOKIE = "muster_session"
SESSION_LIFETIME_SECONDS = 12 * 3600


def load_or_create_token(root: Path) -> tuple[str, str]:
    """The login token and a human description of where it is stored.

    Prefers the OS keyring; falls back to an owner-only file beside
    muster.yaml when no keyring backend is available. The token is
    registered for redaction either way.
    """
    token, where = _keyring_token()
    if token is None:
        token, where = _file_token(root)
    register_secret(token)
    return token, where


def _keyring_token() -> tuple[str | None, str]:
    try:
        import keyring  # noqa: PLC0415 — optional backend, imported lazily
        from keyring.errors import KeyringError  # noqa: PLC0415
    except ImportError:
        return None, ""
    try:
        stored = keyring.get_password(KEYRING_SERVICE, TOKEN_KEYRING_NAME)
        if stored:
            return stored, f"the OS keyring ({KEYRING_SERVICE}/{TOKEN_KEYRING_NAME})"
        token = secrets.token_urlsafe(32)
        keyring.set_password(KEYRING_SERVICE, TOKEN_KEYRING_NAME, token)
        return token, f"the OS keyring ({KEYRING_SERVICE}/{TOKEN_KEYRING_NAME})"
    except KeyringError as exc:
        logger.warning("keyring unavailable for the web token: %s", exc)
        return None, ""


def _file_token(root: Path) -> tuple[str, str]:
    path = root / TOKEN_FILE
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token, str(path)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
    return token, str(path)


@dataclass
class Session:
    csrf: str
    expires_at: float


@dataclass
class SessionStore:
    """In-memory sessions: restart the server and everyone logs in again."""

    sessions: dict[str, Session] = field(default_factory=dict)

    def create(self) -> tuple[str, Session]:
        session_id = secrets.token_urlsafe(32)
        session = Session(
            csrf=secrets.token_urlsafe(32),
            expires_at=time.monotonic() + SESSION_LIFETIME_SECONDS,
        )
        self.sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at < time.monotonic():
            del self.sessions[session_id]
            return None
        return session

    def drop(self, session_id: str | None) -> None:
        if session_id:
            self.sessions.pop(session_id, None)


class RateLimiter:
    """A sliding-window limiter keyed by client address."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def token_matches(presented: str, expected: str) -> bool:
    return secrets.compare_digest(presented.strip(), expected)
