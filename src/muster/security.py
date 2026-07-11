"""Guards for handling untrusted input files.

Every file Muster reads is treated as untrusted: paths are resolved and
confined to a configured root so crafted names or symlinks cannot escape it,
and a size limit rejects files large enough to exhaust memory.
"""

from __future__ import annotations

from pathlib import Path

SUPPORTED_SUFFIXES = frozenset({".csv", ".xlsx"})


class SecurityError(RuntimeError):
    """Raised when an input file fails a safety check."""


def ensure_within(path: Path, root: Path) -> Path:
    """Resolve ``path`` and confirm it stays inside ``root``.

    Returns the resolved path. Raises :class:`SecurityError` on traversal.
    """
    resolved = path.resolve()
    resolved_root = root.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise SecurityError(f"path '{resolved}' escapes '{resolved_root}'")
    return resolved


def ensure_size_within(path: Path, max_mb: int) -> None:
    """Raise :class:`SecurityError` if ``path`` exceeds ``max_mb`` megabytes."""
    size = path.stat().st_size
    limit = max_mb * 1024 * 1024
    if size > limit:
        raise SecurityError(
            f"file '{path.name}' is {size} bytes, over the {max_mb} MB limit"
        )
