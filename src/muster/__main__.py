"""Allow ``python -m muster`` — used by the daemon to re-invoke itself."""

from muster.cli import app

app()
