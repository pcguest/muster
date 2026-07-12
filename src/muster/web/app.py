"""The FastAPI application behind ``muster serve``.

Local-first and single-user: every page requires the login token, sessions
live in memory, all mutating routes demand a per-session CSRF token and are
rate-limited, and every response carries strict security headers (a CSP
with no script sources beyond a per-request nonce — the pages ship no
scripts at all — plus nosniff, no-referrer and frame denial). Pages are
server-rendered Jinja2 with one hand-written stylesheet: no CDNs, no build
step, nothing fetched from anywhere.

All mutations act on append-only or reviewable artefacts: exception
decisions append to the resolutions audit log, mapping decisions update the
Goal 3 review file, and the run button starts the same pipeline the CLI
runs — history is never rewritten from a browser.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from muster import __version__
from muster.assist import REVIEW_FILE_NAME, load_review_file, write_review_file
from muster.config import Config, load_config
from muster.scheduler import run_once
from muster.web import data as web_data
from muster.web.auth import (
    SESSION_COOKIE,
    SESSION_LIFETIME_SECONDS,
    RateLimiter,
    Session,
    SessionStore,
    load_or_create_token,
    token_matches,
)

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent

_FLASH_MESSAGES = {
    "run-started": "Pipeline run started; refresh in a moment.",
    "already-running": "A run is already in progress.",
    "resolved": "Exception marked resolved; the decision is in the audit log.",
    "dismissed": "Exception dismissed; the decision is in the audit log.",
    "accepted": "Mapping accepted; it applies from the next run.",
    "rejected": "Mapping rejected.",
    "logged-out": "Logged out.",
}

_REPORT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)


class RunState:
    """Whether a pipeline run started from the browser is in flight."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.last_exit: int | None = None
        self.last_started: str | None = None

    def try_start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.last_started = datetime.now(UTC).isoformat(timespec="seconds")
            return True

    def finish(self, exit_code: int) -> None:
        with self._lock:
            self.running = False
            self.last_exit = exit_code


def create_app(root: Path, config_path: Path) -> FastAPI:
    """Build the web application over one Muster project."""
    root = root.resolve()
    config_path = config_path.resolve()
    token, token_home = load_or_create_token(root)

    app = FastAPI(title="Muster", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token_home = token_home
    sessions = SessionStore()
    login_limiter = RateLimiter(limit=5, window_seconds=60)
    mutation_limiter = RateLimiter(limit=30, window_seconds=60)
    run_state = RunState()

    environment = Environment(
        loader=FileSystemLoader(_PACKAGE_DIR / "templates"),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )
    app.mount(
        "/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static"
    )

    def fresh_config() -> Config:
        return load_config(config_path)

    def render(
        request: Request, template: str, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        session = sessions.get(request.cookies.get(SESSION_COOKIE))
        page = environment.get_template(template).render(
            version=__version__,
            csrf=session.csrf if session else "",
            flash=_FLASH_MESSAGES.get(str(request.query_params.get("msg", ""))),
            run_state=run_state,
            **context,
        )
        return HTMLResponse(page, status_code=status_code)

    def page_session(request: Request) -> Session | None:
        return sessions.get(request.cookies.get(SESSION_COOKIE))

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def guard_mutation(request: Request, csrf_token: str) -> Response | None:
        """Auth, CSRF and rate-limit checks shared by every mutating route."""
        session = page_session(request)
        if session is None:
            return Response("authentication required", status_code=401)
        if not mutation_limiter.allow(client_key(request)):
            return Response("rate limit exceeded; slow down", status_code=429)
        if not secrets.compare_digest(csrf_token, session.csrf):
            return Response("invalid CSRF token", status_code=403)
        return None

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; img-src 'self'; "
            f"script-src 'nonce-{nonce}'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    # -- authentication ----------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return render(request, "login.html", token_home=token_home, error=None)

    @app.post("/login")
    def login(
        request: Request,
        token_value: Annotated[str, Form(alias="token", max_length=200)],
    ) -> Response:
        if not login_limiter.allow(client_key(request)):
            return Response("rate limit exceeded; slow down", status_code=429)
        if not token_matches(token_value, token):
            logger.warning("failed login from %s", client_key(request))
            return render(
                request,
                "login.html",
                status_code=401,
                token_home=token_home,
                error="That token does not match.",
            )
        session_id, _ = sessions.create()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_LIFETIME_SECONDS,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/logout")
    def logout(
        request: Request,
        csrf_form: Annotated[str, Form(alias="csrf", max_length=200)] = "",
    ) -> Response:
        denied = guard_mutation(request, csrf_form)
        if denied is not None:
            return denied
        sessions.drop(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login?msg=logged-out", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # -- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        if page_session(request) is None:
            return RedirectResponse("/login", status_code=303)
        report = web_data.latest_report(root)
        trends = web_data.run_trends(root)
        peak = max((t.rows_in for t in trends), default=0)
        return render(
            request,
            "dashboard.html",
            active="dashboard",
            report=report,
            trends=trends,
            peak_rows=peak or 1,
        )

    @app.get("/exceptions", response_class=HTMLResponse)
    def exceptions_page(
        request: Request,
        severity: str = "",
        kind: str = "",
        file: str = "",
    ) -> Response:
        if page_session(request) is None:
            return RedirectResponse("/login", status_code=303)
        for value in (severity, kind, file):
            if len(value) > 200:
                return Response("filter value too long", status_code=422)
        rows = web_data.load_exceptions(root, fresh_config())
        kinds = sorted({r.kind for r in rows})
        files = sorted({r.file for r in rows})
        if severity:
            rows = [r for r in rows if r.severity == severity]
        if kind:
            rows = [r for r in rows if r.kind == kind]
        if file:
            rows = [r for r in rows if r.file == file]
        return render(
            request,
            "exceptions.html",
            active="exceptions",
            rows=rows,
            kinds=kinds,
            files=files,
            severity=severity,
            kind=kind,
            file=file,
        )

    @app.post("/exceptions/{exception_id}")
    def resolve_exception(
        request: Request,
        exception_id: Annotated[str, PathParam(pattern=r"^[0-9a-f]{16}$")],
        action: Annotated[str, Form(max_length=20)],
        csrf_form: Annotated[str, Form(alias="csrf", max_length=200)] = "",
        note: Annotated[str, Form(max_length=web_data.MAX_NOTE_LENGTH)] = "",
    ) -> Response:
        denied = guard_mutation(request, csrf_form)
        if denied is not None:
            return denied
        if action not in web_data.RESOLUTION_ACTIONS:
            return Response("action must be 'resolved' or 'dismissed'", status_code=422)
        known = {r.id for r in web_data.load_exceptions(root, fresh_config())}
        if exception_id not in known:
            return Response("no such exception in the latest run", status_code=404)
        web_data.append_resolution(root, exception_id, action, note.strip())
        return RedirectResponse(f"/exceptions?msg={action}", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request) -> Response:
        if page_session(request) is None:
            return RedirectResponse("/login", status_code=303)
        review_path = root / REVIEW_FILE_NAME
        review = load_review_file(review_path) if review_path.is_file() else None
        return render(request, "review.html", active="review", review=review)

    @app.post("/review/{index}")
    def decide_mapping(
        request: Request,
        index: Annotated[int, PathParam(ge=0, le=9999)],
        action: Annotated[str, Form(max_length=20)],
        csrf_form: Annotated[str, Form(alias="csrf", max_length=200)] = "",
    ) -> Response:
        denied = guard_mutation(request, csrf_form)
        if denied is not None:
            return denied
        if action not in ("accept", "reject"):
            return Response("action must be 'accept' or 'reject'", status_code=422)
        review_path = root / REVIEW_FILE_NAME
        if not review_path.is_file():
            return Response("no review file; run with --assist first", status_code=404)
        review = load_review_file(review_path)
        if index >= len(review.proposals):
            return Response("no such proposal", status_code=404)
        proposal = review.proposals[index]
        if proposal.status != "pending":
            return RedirectResponse("/review", status_code=303)
        known = {spec.name for spec in fresh_config().fields}
        if action == "accept" and proposal.target in known:
            proposal.status = "accepted"
            outcome = "accepted"
        else:
            proposal.status = "rejected"
            outcome = "rejected"
        write_review_file(review, review_path)
        return RedirectResponse(f"/review?msg={outcome}", status_code=303)

    @app.post("/run")
    def trigger_run(
        request: Request,
        csrf_form: Annotated[str, Form(alias="csrf", max_length=200)] = "",
    ) -> Response:
        denied = guard_mutation(request, csrf_form)
        if denied is not None:
            return denied
        if not run_state.try_start():
            return RedirectResponse("/?msg=already-running", status_code=303)

        def worker() -> None:
            try:
                run_state.finish(run_once(root, config_path))
            except Exception:
                logger.exception("browser-triggered run crashed")
                run_state.finish(-1)

        threading.Thread(target=worker, name="muster-run", daemon=True).start()
        return RedirectResponse("/?msg=run-started", status_code=303)

    @app.get("/report")
    def report_page(request: Request) -> Response:
        if page_session(request) is None:
            return RedirectResponse("/login", status_code=303)
        report_path = root / fresh_config().output.directory / "report.html"
        if not report_path.is_file():
            return Response("no report yet; run the pipeline first", status_code=404)
        # The archived report carries its own inline styles; scope a CSP that
        # allows exactly that and nothing else.
        return HTMLResponse(
            report_path.read_text(encoding="utf-8"),
            headers={"Content-Security-Policy": _REPORT_CSP},
        )

    return app
