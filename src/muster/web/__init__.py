"""The Muster web interface: a local-first dashboard over one project.

Server-rendered pages only — Jinja2 templates, one hand-written stylesheet,
no scripts, no CDNs, no build step. See :mod:`muster.web.app` for the
routes and :mod:`muster.web.auth` for the security model.
"""

from muster.web.app import create_app

__all__ = ["create_app"]
