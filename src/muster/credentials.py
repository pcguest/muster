"""Secret handling: resolution, registration and redaction.

This is the confidentiality leg of the CIA triad. Secrets never live in
muster.yaml — target configurations name *environment variables*, and the
value is resolved only at publish time: from the environment first, then,
if the variable is unset and the optional ``keyring`` library is installed,
from the operating system keyring (service ``muster``, username = the
variable name, i.e. ``keyring set muster MUSTER_PG_DSN``).

Every resolved secret is registered in this process, and both the logging
pipeline and publish output are passed through :func:`redact_text`, so a
resolved secret cannot appear in terminal output, log lines or manifests —
including inside larger strings such as connection URLs or tracebacks.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "muster"
REDACTED = "[redacted]"

# Environment variable names are configuration, not secrets; constrain their
# shape so a secret value cannot be smuggled into an *_env config field.
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

_registry: set[str] = set()


class SecretError(RuntimeError):
    """Raised when a required secret cannot be resolved."""


def register_secret(value: str) -> None:
    """Remember a secret value so redaction can strip it from any output."""
    if value and len(value) >= 4:
        _registry.add(value)


def clear_registered_secrets() -> None:
    """Forget all registered secrets (used by tests)."""
    _registry.clear()


def resolve_secret(env_name: str, purpose: str) -> str:
    """Resolve a secret named by an environment variable.

    Looks in the environment first, then in the OS keyring if the optional
    ``keyring`` library is installed. The resolved value is registered for
    redaction before being returned. Raises :class:`SecretError` when the
    secret cannot be found — the error names where to put it, never what it
    holds.
    """
    value = os.environ.get(env_name, "").strip()
    if not value:
        value = _from_keyring(env_name)
    if not value:
        raise SecretError(
            f"no secret found for {purpose}: set the {env_name} environment "
            f"variable, or store it in the OS keyring with "
            f"'keyring set {KEYRING_SERVICE} {env_name}' (pip install keyring)"
        )
    register_secret(value)
    return value


def _from_keyring(env_name: str) -> str:
    try:
        import keyring  # noqa: PLC0415 — optional dependency, imported lazily
    except ImportError:
        return ""
    try:
        stored = keyring.get_password(KEYRING_SERVICE, env_name)
    except Exception as exc:  # a broken backend must not crash publishing
        logger.warning("keyring lookup failed for %s: %s", env_name, exc)
        return ""
    return (stored or "").strip()


def redact_text(text: str) -> str:
    """Replace every registered secret occurring in ``text``.

    Longest secrets are replaced first so one secret embedded in another
    (e.g. a password inside a DSN) cannot leave a fragment behind.
    """
    for secret in sorted(_registry, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class SecretRedactingFilter(logging.Filter):
    """A logging filter that strips registered secrets from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _registry:
            message = record.getMessage()
            redacted = redact_text(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        return True
