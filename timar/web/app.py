"""The web layer: setup, login, and a read-only fleet dashboard.

Phase 1 is deliberately read-only. Timar can already wake, update and sweep — exposing those
through a web form is a separate step from being able to *see* the fleet, and the seeing is what
was missing. Settings editing and manual actions come next.

Server-rendered Jinja with HTMX for the live bits: this is one operator looking at a table of
machines, and a single-page app would add a build step, a second container and a CSP to a
product whose whole shape is "one image, one volume".
"""
from __future__ import annotations

from pathlib import Path

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, jobs, state, status as fleet_status
from ..scheduler import scheduler
from . import auth, settings
from .auth import require_operator as current_operator

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    """The scheduler runs in this process, in this event loop.

    One process means one PID, one log stream, and `restart: unless-stopped` meaning what it
    says. The cost is that a crashed task would vanish silently — which is what the supervisor
    and the heartbeats in `scheduler.py` exist to make visible.
    """
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="Timar", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.include_router(settings.router)


def _set_session(response, username: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_token(username),
        httponly=True,      # invisible to any script on the page
        samesite="lax",     # a cross-site form POST cannot ride this cookie
        max_age=auth.SESSION_DAYS * 86400,
        # Deliberately not `secure`: the documented deployment is a private network, often
        # plain http, and a `secure` cookie is silently dropped there — which presents as "the
        # login form works but I am never logged in". Terminate TLS in front of it if you want
        # transport security; that is the reverse proxy's job, not this app's.
    )


@app.exception_handler(status.HTTP_401_UNAUTHORIZED)
async def unauthorized(request: Request, exc: HTTPException):
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.middleware("http")
async def force_setup_first(request: Request, call_next):
    """Until an account exists, every path leads to /setup and nothing else answers.

    Otherwise the window between first boot and the operator finishing setup is a window in
    which the dashboard — the fleet's inventory — is served to anyone who asks.
    """
    path = request.url.path
    if not config.is_configured() and not (
        path in ("/setup", "/health") or path.startswith("/static/")
    ):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    if config.is_configured() and path == "/setup":
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return await call_next(request)


@app.get("/health")
async def health():
    """Unauthenticated on purpose: it reveals liveness and nothing about the fleet."""
    return {"status": "ok", "configured": config.is_configured()}


@app.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    return TEMPLATES.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup")
async def setup_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        auth.create_account(username, password)
    except auth.AuthError as e:
        return TEMPLATES.TemplateResponse(
            request, "setup.html", {"error": str(e)}, status_code=status.HTTP_400_BAD_REQUEST
        )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session(response, username)
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        auth.verify(username, password)
    except auth.AuthError as e:
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": str(e)}, status_code=status.HTTP_401_UNAUTHORIZED
        )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session(response, username)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


def _job_view() -> list[dict]:
    """What the dashboard shows for each job.

    `last_run` is the load-bearing field. A job that stops being scheduled writes no error
    anywhere — a stale timestamp is the only thing that reveals it.
    """
    cfg = config.load()
    schedules = cfg.get("schedules") or {}
    from .. import schedule as schedule_module

    rows = []
    for name in jobs.JOBS:
        record = state.job(name)
        spec = schedule_module.Schedule.from_dict(schedules.get(name))
        rows.append({
            "name": name,
            "title": jobs.TITLES[name],
            "schedule": spec.describe(),
            "running": scheduler.is_running(name),
            "status": record.get("status"),
            "last_run": record.get("last_run"),
            "last_summary": record.get("last_summary"),
            "last_error": record.get("last_error"),
            "next_run": record.get("next_run"),
            "heartbeat": state.heartbeats().get(f"job:{name}"),
            "has_report": bool(record.get("last_report")),
        })
    return rows


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, operator: str = Depends(current_operator)):
    cfg = config.load()
    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "operator": operator,
        "servers": cfg.get("servers", []),
        "fleet": fleet_status.fleet(cfg),
        "jobs": _job_view(),
    })


@app.get("/fragments/fleet", response_class=HTMLResponse)
async def fleet_fragment(request: Request, operator: str = Depends(current_operator)):
    """The status table alone — polled by HTMX so the page updates without a reload."""
    return TEMPLATES.TemplateResponse(request, "_fleet.html", {
        "fleet": fleet_status.fleet(config.load()),
    })


@app.get("/fragments/jobs", response_class=HTMLResponse)
async def jobs_fragment(request: Request, operator: str = Depends(current_operator)):
    return TEMPLATES.TemplateResponse(request, "_jobs.html", {"jobs": _job_view()})


@app.post("/jobs/{name}/run", response_class=HTMLResponse)
async def run_job(request: Request, name: str, operator: str = Depends(current_operator)):
    """Start a job now.

    A scheduler you cannot trigger is a scheduler you cannot verify — the operator has to be
    able to prove the plumbing works without waiting a week for the next window.

    Returns immediately with the panel; the work runs in the background and the panel's own
    polling reports it. Waiting for an update run to finish would hold the request open for ten
    minutes and time out at every proxy in between.
    """
    if name not in jobs.JOBS:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    asyncio.create_task(scheduler.run(name))
    # Let the job mark itself as started before the panel is rendered, so the first response
    # already shows "running" rather than a stale idle state the operator has to wait out.
    await asyncio.sleep(0.05)
    return TEMPLATES.TemplateResponse(request, "_jobs.html", {"jobs": _job_view()})


@app.get("/jobs/{name}/report", response_class=HTMLResponse)
async def job_report(request: Request, name: str, operator: str = Depends(current_operator)):
    """The full findings behind a job's one-line summary.

    A page of its own rather than an expander in the job table: that panel re-renders every
    fifteen seconds, so anything opened inside it would close again while being read.

    Notifications are not the only copy of a report. Before this existed, an installation with
    no Telegram token swept its fleet and discarded every finding, leaving a dashboard that
    said "1 with findings" and no way to find out which.
    """
    if name not in jobs.JOBS:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    record = state.job(name)
    return TEMPLATES.TemplateResponse(request, "report.html", {
        "title": jobs.TITLES[name],
        "report": record.get("last_report") or "",
        "last_run": record.get("last_run"),
        "summary": record.get("last_summary"),
        "error": record.get("last_error"),
    })
