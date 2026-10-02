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
import logging
from pathlib import Path

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, i18n, jobs, power, reports, state, status as fleet_status
from .. import containers as container_status
from ..i18n import gettext as _
from ..scheduler import scheduler
from . import auth, outcome, settings
from .auth import require_operator as current_operator

HERE = Path(__file__).parent
logger = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
i18n.install(TEMPLATES.env)
outcome.install(TEMPLATES.env)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """The scheduler runs in this process, in this event loop.

    One process means one PID, one log stream, and `restart: unless-stopped` meaning what it
    says. The cost is that a crashed task would vanish silently — which is what the supervisor
    and the heartbeats in `scheduler.py` exist to make visible.
    """
    scheduler.start()
    refresher = asyncio.create_task(_keep_status_fresh())
    try:
        yield
    finally:
        refresher.cancel()
        await scheduler.stop()


async def _keep_status_fresh() -> None:
    """Probe the fleet and read the containers in the background, so no page waits for either.

    The pages read whatever this found last; see `status` for what happens if it stops.
    """
    while True:
        try:
            cfg = config.load()
            await asyncio.to_thread(fleet_status.refresh, cfg)
            await asyncio.to_thread(container_status.refresh, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background status refresh failed")
        await asyncio.sleep(fleet_status.REFRESH_EVERY)


app = FastAPI(title="timar", docs_url=None, redoc_url=None, lifespan=lifespan)
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
        path in ("/setup", "/health", "/lang") or path.startswith("/static/")
    ):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    if config.is_configured() and path == "/setup":
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return await call_next(request)


# Registered after `force_setup_first`, so it runs *before* it: Starlette wraps each new
# middleware around the ones already added. The language has to be settled before anything
# renders, including the setup page that the redirect above leads to.
@app.middleware("http")
async def choose_language(request: Request, call_next):
    i18n.activate(i18n.negotiate(request.cookies.get(i18n.COOKIE),
                                 request.headers.get("accept-language")))
    return await call_next(request)


def _supported_language(code: str) -> str | None:
    """The catalog's own key for `code`, or None — so the caller never holds the request value."""
    return next((lang for lang in i18n.LANGUAGES if lang == code), None)


def _local_path(target: str) -> str:
    """`target` as a path on this site, or "/" — the redirect never starts with request text.

    The location is rebuilt as a constant "/" followed by the target with its leading slashes
    removed, so no value can make it scheme-relative (`//host`). Backslashes, whitespace and
    control characters are refused outright: browsers read `\\` as `/` and drop tabs and
    newlines from a URL, which is how `/\\host` or `/<TAB>/host` becomes `//host` after a
    prefix check has already passed.
    """
    if not target.startswith("/") or any(
        c == "\\" or c.isspace() or not c.isprintable() for c in target
    ):
        return "/"
    return "/" + target.lstrip("/")


@app.get("/lang")
async def set_language(code: str = "", next: str = "/"):
    """Remember a language choice for this browser and go back to where it was made.

    Open before setup and before login: the person who cannot read the setup page is the one who
    most needs the switch. `next` must be a path on this site — anything else would turn the
    route into an open redirect.
    """
    response = RedirectResponse(_local_path(next), status_code=status.HTTP_303_SEE_OTHER)
    # The cookie is written from the catalog's own key, never from the query: a code that is
    # not a supported language sets nothing, and nothing the request carries reaches the
    # Set-Cookie header — not even a value that was checked first.
    if (lang := _supported_language(code)) is not None:
        response.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, samesite="lax")
    return response


# HEAD as well as GET: uptime monitors (homepage's siteMonitor among them) probe with HEAD
# first, and a 405 there is logged on every check and costs a second request to recover from.
@app.api_route("/health", methods=["GET", "HEAD"])
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
            "title": _(jobs.TITLES[name]),
            "schedule": spec.describe(),
            "running": scheduler.is_running(name),
            "stopping": scheduler.is_stopping(name),
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


def _fleet_view(cfg: dict, sort: str | None, dir: str | None) -> dict:
    """The fleet table, ordered by the column the operator picked.

    The order is a query parameter, not something the browser keeps: the table is re-fetched
    every ten seconds, and a sort applied in the page would be undone by the next poll. As a URL
    it also survives a refresh and a bookmark. Anything unrecognised falls back to the name.
    """
    key = sort if sort in fleet_status.SORT_KEYS else "name"
    descending = dir == "desc"
    return {
        "fleet": fleet_status.sort_fleet(fleet_status.fleet(cfg), key, descending),
        "sort": key,
        "descending": descending,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, sort: str | None = None, dir: str | None = None,
                    operator: str = Depends(current_operator)):
    cfg = config.load()
    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "servers": cfg.get("servers", []),
        **_fleet_view(cfg, sort, dir),
    })


@app.get("/fragments/fleet", response_class=HTMLResponse)
async def fleet_fragment(request: Request, sort: str | None = None, dir: str | None = None,
                         operator: str = Depends(current_operator)):
    """The status table alone — polled by HTMX so the page updates without a reload."""
    return TEMPLATES.TemplateResponse(request, "_fleet.html",
                                      _fleet_view(config.load(), sort, dir))


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


@app.post("/jobs/{name}/stop", response_class=HTMLResponse)
async def stop_job(request: Request, name: str, operator: str = Depends(current_operator)):
    """Ask a running job to stop after the step it is on — see `cancel` for why not at once."""
    if name not in jobs.JOBS:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    scheduler.request_stop(name)
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
    return _report_response(request, {
        "title": _(jobs.TITLES[name]),
        "subtitle": _("Last run"),
        "report": record.get("last_report") or "",
        "last_run": record.get("last_run"),
        "summary": record.get("last_summary"),
        "error": record.get("last_error"),
    })


def _report_response(request: Request, context: dict):
    """A report as the dialog's panel when htmx asks, as a page of its own otherwise."""
    if request.headers.get("HX-Request") == "true":
        return TEMPLATES.TemplateResponse(request, "_report.html", {**context, "in_dialog": True})
    return TEMPLATES.TemplateResponse(request, "report.html", {**context, "in_dialog": False})


def _filters(selected: str | None) -> list[dict]:
    """The dropdown's options: every job, plus everything.

    Built from `jobs.JOBS` rather than from what the archive happens to contain, so a job that
    has never run is still offered — and its empty list is the answer to "why have I seen no
    update report", which is a question the filter should be able to ask.
    """
    tally = reports.counts()
    options = [{"value": "", "label": _("All reports"), "count": sum(tally.values())}]
    options += [{"value": name, "label": _(jobs.TITLES[name]), "count": tally.get(name, 0)}
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
    # Scheduled work sits at the top of this page: the latest run of each job, then every run
    # before it.
    return TEMPLATES.TemplateResponse(request, "reports.html", {
        "jobs": _job_view(),
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
    return HTMLResponse(
        f'<span class="ok">{html.escape(_("{message} — the table follows.", message=message))}</span>')


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
    return _report_response(request, {
        # The stored title is the English one, written when the run finished; translated on the
        # way out so the archive follows the reader's language rather than the writer's.
        "title": _(entry.get("title") or entry.get("job", "Report")),
        "subtitle": _("Archived run"),
        "report": entry.get("report") or "",
        "last_run": entry.get("finished_at"),
        "summary": entry.get("summary"),
        "error": entry.get("error"),
    })


# -- containers ----------------------------------------------------------------------------

async def _containers_view(sort: str | None, dir: str | None) -> dict:
    """The containers table in the order picked — in the URL, like the servers table's."""
    key = sort if sort in container_status.SORT_KEYS else "name"
    descending = dir == "desc"
    projects = await asyncio.to_thread(container_status.projects, config.load())
    return {"projects": container_status.sort_projects(projects, key, descending),
            "sort": key, "descending": descending}


@app.get("/containers", response_class=HTMLResponse)
async def containers_page(request: Request, sort: str | None = None, dir: str | None = None,
                          operator: str = Depends(current_operator)):
    return TEMPLATES.TemplateResponse(request, "containers.html", await _containers_view(sort, dir))


@app.get("/fragments/containers", response_class=HTMLResponse)
async def containers_fragment(request: Request, sort: str | None = None, dir: str | None = None,
                              operator: str = Depends(current_operator)):
    """The table alone, polled by HTMX. In a thread: it may open an SSH session per host."""
    return TEMPLATES.TemplateResponse(request, "_containers.html", await _containers_view(sort, dir))


@app.post("/containers/{name}/{action}", response_class=HTMLResponse)
async def container_action(name: str, action: str, operator: str = Depends(current_operator)):
    """Start, stop or restart one project, answered as a sentence like the power buttons."""
    if action not in container_status.ACTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    cfg = config.load()
    entry = next((c for c in cfg.get("containers") or [] if c["name"] == name), None)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        message = await asyncio.to_thread(container_status.act, entry, cfg.get("servers", []), action)
    except container_status.ContainerError as e:
        return HTMLResponse(f'<span class="error">{html.escape(str(e))}</span>')
    container_status.invalidate()
    return HTMLResponse(
        f'<span class="ok">{html.escape(_("{message} — the table follows.", message=message))}</span>')
