"""The web layer: setup, login, and the fleet dashboard.

Server-rendered Jinja with HTMX for the live bits: this is one operator looking at a table of
machines, and a single-page app would add a build step, a second container and a CSP to a
product whose whole shape is "one image, one volume".

The dashboard is not read-only. Seeing that a machine is asleep and having to go elsewhere to
wake it is the same trip an operator makes all day, so the two power actions live on the row
that reports the state — see the power routes at the end of this file.
"""
from __future__ import annotations

import html
from pathlib import Path

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, jobs, power, reports, state, status as fleet_status
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

    # Counted once for the whole table rather than per row: this view is re-rendered every
    # fifteen seconds by the panel's own polling.
    archived = reports.counts()
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
            "archived": archived.get(name, 0),
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
        "subtitle": "Last run",
        "report": record.get("last_report") or "",
        "last_run": record.get("last_run"),
        "summary": record.get("last_summary"),
        "error": record.get("last_error"),
        "back": "/",
        "back_label": "dashboard",
    })


def _filters(selected: str | None) -> list[dict]:
    """The dropdown's options: every job, plus everything.

    Built from `jobs.JOBS` rather than from what the archive happens to contain, so a job that
    has never run is still offered — and its empty list is the answer to "why have I seen no
    update report", which is a question the filter should be able to ask.
    """
    tally = reports.counts()
    options = [{"value": "", "label": "All reports", "count": sum(tally.values())}]
    options += [{"value": name, "label": jobs.TITLES[name], "count": tally.get(name, 0)}
                for name in jobs.JOBS]
    for option in options:
        option["selected"] = option["value"] == (selected or "")
    return options


@app.get("/reports", response_class=HTMLResponse)
async def report_archive(request: Request, job: str = "",
                         operator: str = Depends(current_operator)):
    """Every report a job has produced, not just the most recent one.

    The dashboard answers "what did the last sweep find". This answers "when did it start" —
    a disk creeping past 90%, a host that has been unreachable for three sweeps, an update
    failing every Friday. None of those are visible in a single snapshot.

    There is no fragment route beside this one, unlike the polled panels: the filter re-requests
    this page and HTMX takes the list out of the response. A one-off click can afford the whole
    page, and it keeps the URL in the address bar a real one that survives a refresh.

    An unknown job filters to nothing rather than 404s — the value comes from a dropdown, and a
    stale bookmark naming a job that no longer exists should show an empty list, not an error.
    """
    return TEMPLATES.TemplateResponse(request, "reports.html", {
        "job": job,
        "filters": _filters(job),
        "reports": reports.listing(job or None),
    })


def _power_target(name: str) -> tuple[dict, list[dict]]:
    servers = config.load().get("servers", [])
    server = next((s for s in servers if s["name"] == name), None)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return server, servers


async def _power(name: str, action) -> HTMLResponse:
    """Run one power action and report it as a sentence.

    In a thread, like every other blocking call in this codebase: paramiko is synchronous and a
    `qm shutdown` waits for the guest to stop. On the event loop that would freeze the scheduler
    and every other browser tab for the duration — including the polling that is the operator's
    only evidence the action worked.

    The message is escaped because it carries SSH and hypervisor error output verbatim.
    """
    server, servers = _power_target(name)
    try:
        message = await asyncio.to_thread(action, server, servers)
    except power.PowerError as e:
        return HTMLResponse(f'<span class="error">{html.escape(str(e))}</span>')
    # The machine is about to change state and the cached probe is now a lie; the next poll
    # should show the truth rather than a ten-second-old snapshot of it.
    fleet_status.invalidate()
    return HTMLResponse(f'<span class="ok">{html.escape(message)} — the table follows.</span>')


@app.post("/servers/{name}/wake", response_class=HTMLResponse)
async def wake_server(name: str, operator: str = Depends(current_operator)):
    """Wake a machine now.

    Waking is the one operation with no feedback of its own — a magic packet is fire and forget,
    and a machine that stays dark could be a wrong MAC, a packet that never left the host, or
    Wake-on-LAN disabled in firmware. Pressing the button and watching the row is how an operator
    tells those apart.
    """
    return await _power(name, power.wake)


@app.post("/servers/{name}/shutdown", response_class=HTMLResponse)
async def shutdown_server(name: str, operator: str = Depends(current_operator)):
    return await _power(name, power.shutdown)


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def archived_report(request: Request, report_id: str,
                          operator: str = Depends(current_operator)):
    entry = reports.get(report_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return TEMPLATES.TemplateResponse(request, "report.html", {
        "title": entry.get("title") or entry.get("job", "Report"),
        "subtitle": "Archived run",
        "report": entry.get("report") or "",
        "last_run": entry.get("finished_at"),
        "summary": entry.get("summary"),
        "error": entry.get("error"),
        # Back to the filtered list this was almost certainly reached from, not to the whole
        # archive: an operator comparing four update runs should not re-pick the filter between
        # each one.
        "back": f"/reports?job={entry.get('job', '')}",
        "back_label": "reports",
    })
