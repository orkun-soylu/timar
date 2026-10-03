"""Settings: servers, log sweep defaults, the model connection, and notifications.

The settings page itself carries only the fleet-wide settings. Servers are managed from the
dashboard: its buttons open the add/edit form — which is also where a server is enrolled — in a dialog, filled from
the routes here — which answer with the bare panel to htmx and with a whole page otherwise, so
every button is still a working link without scripting.

Every route here rewrites `config.yaml` through `config.save()`, which replaces the file
atomically — the scheduler may be reading it at the same moment.

**Secrets are never sent to the browser.** The forms show whether a key is stored, not what it
is, and an empty key field means "leave it as it was". That has to be the rule rather than
"empty means delete", because a blank box is the *normal* state of the field on every visit —
treating it as a deletion would wipe the credential on any unrelated edit to the same form.
"""
from __future__ import annotations

import asyncio
import html
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import containers as container_status, membership
from .. import (config, enroll as enroll_module, i18n, jobs, keys, llm as llm_module, notify,
                release, state, status as fleet_status, updater, validate)
from ..scheduler import scheduler
from ..i18n import gettext as _
from ..platforms import PLATFORMS, get as get_platform
from ..schedule import DAYS as _DAYS, KINDS as _KINDS
from . import assets, outcome
from .. import osinfo as osinfo_module
from .auth import require_operator

# Every route in this file is behind the session guard. Declared once on the router rather than
# per-route: a new settings endpoint should be protected because it is a settings endpoint, not
# because whoever added it remembered a decorator.
router = APIRouter(prefix="/settings", dependencies=[Depends(require_operator)])
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
i18n.install(TEMPLATES.env)
outcome.install(TEMPLATES.env)
assets.install(TEMPLATES.env)

SEE_OTHER = 303


def _escape(text: str) -> str:
    """These strings carry third-party error bodies straight into the page."""
    return html.escape(text)


def _server_values(servers: list[dict], guest_of: dict, edit: str | None,
                   submitted: dict | None) -> dict:
    """What the add/edit form renders with, in priority order.

    **What was typed wins.** Before this, a rejected form was re-rendered from *storage*: an add
    came back empty and an edit came back showing the values already saved, so the operator was
    told what was wrong with input that was no longer on the screen — and a long edit had to be
    retyped from memory. The stored entry is only the starting point for a form nobody has
    submitted yet.

    The guest link is flattened into the same dict because the form is flat: it lives on the
    hypervisor's entry as `manages_vms`, not on the guest's, and the two form fields have to come
    from somewhere.
    """
    if submitted is not None:
        # The password is never sent back, not even to a form that was rejected: refilled, it
        # would sit in the browser's history and in any proxy in between.
        return {k: v for k, v in submitted.items() if k != "password"}
    if edit:
        server = next((s for s in servers if s["name"] == edit), None)
        if server:
            link = guest_of.get(edit) or {}
            # The box shows what Timar will *do*, not which flag was written: a machine with a
            # MAC, or a guest of an on-demand host, is on-demand without one.
            return {**server,
                    "no_ssh": not config.has_ssh(server),
                    "hypervisor": link.get("hypervisor", ""),
                    "vm_id": link.get("vm_id", ""),
                    "on_demand": edit in config.on_demand(servers)}
    return {}


def _htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _panel(request: Request, template: str, context: dict, *, title: str,
           status_code: int = 200, section: str = "servers"):
    """The panel alone for the dashboard's dialog, or wrapped in a page without scripting.

    Inside the dialog a rejected form comes back as **200**: htmx does not swap an error
    response, so a 400 would leave the dialog showing the form as it was before the save, with
    no word of what was wrong. The page keeps its honest status.
    """
    if _htmx(request):
        return TEMPLATES.TemplateResponse(request, template, {**context, "in_dialog": True})
    return TEMPLATES.TemplateResponse(request, "server_page.html", {
        **context, "in_dialog": False, "body_template": template, "page_title": title,
        "section": section,
    }, status_code=status_code)


def _done(request: Request, to: str = "/"):
    """Back to the list after a save or a removal, whichever way the request came."""
    if _htmx(request):
        # A full reload rather than closing the dialog in place: the list behind it has to show
        # the change, and it is the only thing on the page that would.
        return HTMLResponse("", headers={"HX-Redirect": to})
    return RedirectResponse(to, status_code=SEE_OTHER)


def _guest_of(servers: list[dict]) -> dict:
    return {
        guest["server_name"]: {"hypervisor": host["name"], "vm_id": guest["vm_id"],
                               "guest_on_demand": bool(guest.get("on_demand"))}
        for host in servers
        for guest in host.get("manages_vms", [])
    }


def _server_form(request: Request, *, edit: str | None = None, submitted: dict | None = None,
                 errors: list[str] | None = None, status_code: int = 200,
                 enrol_result: str | None = None, enrol_error: str | None = None):
    servers = config.load().get("servers", [])
    guest_of = _guest_of(servers)
    # The name being edited comes from the submitted form first: a rejected rename still has to
    # re-open as an edit of the *original* entry, or saving again would add a second server.
    editing = (submitted or {}).get("original_name") or edit
    if editing and not any(s["name"] == editing for s in servers):
        editing = None  # the server has been removed since the form was opened
    return _panel(request, "_server_form.html", {
        "servers": servers,
        "editing": editing,
        "values": _server_values(servers, guest_of, editing, submitted),
        "platforms": list(PLATFORMS),
        "device_logos": sorted({**osinfo_module.DEVICE_LOGOS, **osinfo_module.LOGOS}.items(), key=lambda kv: kv[1].casefold()),
        "platform_defaults": [(p.id, p.label, p.default_update_cmd) for p in PLATFORMS.values()],
        "default_update_timeout": updater.DEFAULT_UPDATE_TIMEOUT,
        "min_update_timeout": validate.MIN_UPDATE_TIMEOUT,
        "max_update_timeout": validate.MAX_UPDATE_TIMEOUT,
        "errors": errors or [],
        # The installation's key, for the SSH section's help: the fingerprint to compare and the
        # public half for anyone who would rather install it by hand.
        "fingerprint": keys.fingerprint(),
        "public_key": keys.public_key(),
        "enrol_result": enrol_result,
        "enrol_error": enrol_error,
    }, title=_("Edit {name}", name=editing) if editing else _("Add a server"),
       status_code=status_code)


def _view(request: Request, *, errors: list[str] | None = None, notice: str | None = None,
          status_code: int = 200):
    cfg = config.load()
    llm_cfg = cfg.get("llm") or {}
    telegram_cfg = cfg.get("telegram") or {}
    return TEMPLATES.TemplateResponse(request, "settings.html", {
        "llm": {k: v for k, v in llm_cfg.items() if k != "api_key"},
        "llm_has_key": bool(llm_cfg.get("api_key")),
        "telegram_chat_id": telegram_cfg.get("chat_id", ""),
        "telegram_has_token": bool(telegram_cfg.get("token")),
        "providers": llm_module.PROVIDERS,
        "anthropic_default": llm_module.DEFAULTS[llm_module.ANTHROPIC]["model"],
        "version": release.current_version(),
        "source_url": release.SOURCE_URL,
        "site_url": release.SITE_URL,
        "errors": errors or [],
        "notice": notice,
    }, status_code=status_code)


def _redirect(notice: str | None = None):
    return RedirectResponse("/settings" + (f"?notice={notice}" if notice else ""),
                            status_code=SEE_OTHER)


@router.get("", response_class=HTMLResponse)
async def page(request: Request, notice: str | None = None, edit: str | None = None,
               add: bool = False, enroll: str | None = None):
    # The server list used to live here, opened with these parameters. They are what old
    # bookmarks and browser history point at, so they lead to where that panel lives now.
    # The target is built from the *stored* name, never from the query: only a server that
    # exists is redirected to, and nothing the request carries ends up in a Location header.
    # A link to a server that has since been removed just opens this page.
    names = {s["name"]: s["name"] for s in config.load().get("servers", [])}
    if enroll and enroll in names:
        return RedirectResponse(f"/settings/servers/{quote(names[enroll])}/edit",
                                status_code=SEE_OTHER)
    if edit and edit in names:
        return RedirectResponse(f"/settings/servers/{quote(names[edit])}/edit",
                                status_code=SEE_OTHER)
    if add:
        return RedirectResponse("/settings/servers/new", status_code=SEE_OTHER)
    return _view(request, notice=notice)


@router.get("/servers/new", response_class=HTMLResponse)
async def new_server(request: Request):
    return _server_form(request)


@router.get("/servers/{name}/edit", response_class=HTMLResponse)
async def edit_server(request: Request, name: str):
    _find_server(name)
    return _server_form(request, edit=name)


def _rename_references(servers: list[dict], old: str, new: str) -> None:
    """Carry a rename into the places other servers name this one.

    A server is referred to by name in two places, and a rename that updates only the entry
    itself leaves a guest nothing will ever start and a wake relay that resolves to nobody —
    both of which fail silently, at the next scheduled run, far from the edit that caused them.
    """
    for server in servers:
        for guest in server.get("manages_vms", []):
            if guest["server_name"] == old:
                guest["server_name"] = new
        if server.get("wol_relay") == old:
            server["wol_relay"] = new


def _carry_hand_written(entry: dict, previous: dict) -> dict:
    """Keep the parts of a server entry this form cannot edit.

    `validate.server` builds an entry out of the form, which is right — it is what makes clearing
    a field actually clear it. But the form is not the whole entry: `job_logs`, `watch_logs`,
    `ssh_key` and the `manages_vms` relationship have no input on this page, so a save that wrote
    only what the form knows about **deleted them**.

    Both halves of that failure are silent by construction. A sweep that stops watching a backup
    log reports nothing, because a job nobody is watching writes no error — the exact blindness
    `job_logs` exists to remove. And a hypervisor edited into forgetting its guests leaves a VM
    that `config.on_demand` then calls always-on, so every night it correctly spends powered off
    is reported as an outage.

    Keyed off `validate.SERVER_FIELDS` rather than a list of names to preserve: the question is
    "did the form own this?", and anything else belongs to whoever wrote it.
    """
    for key, value in previous.items():
        if key not in validate.SERVER_FIELDS:
            entry.setdefault(key, value)
    return entry


def _relink_guest(servers: list[dict], name: str, link: tuple[str, int] | None,
                  on_demand: bool = False) -> None:
    """Make `name` a guest of exactly the hypervisor in `link`, or of none at all.

    `on_demand` is written only when set, so an entry that never used it stays as it was.
    """
    for server in servers:
        if guests := server.get("manages_vms"):
            server["manages_vms"] = [g for g in guests if g["server_name"] != name]
            if not server["manages_vms"]:
                del server["manages_vms"]

    if link is None:
        return
    hypervisor, vm_id = link
    for server in servers:
        if server["name"] == hypervisor:
            guest = {"vm_id": vm_id, "server_name": name}
            if on_demand:
                guest["on_demand"] = True
            server.setdefault("manages_vms", []).append(guest)


@router.post("/servers")
async def save_server(request: Request):
    """Add a server, or replace one when `original_name` is present.

    Add and edit share a handler because they share every rule; splitting them is how the two
    paths drift until one of them stops validating something.
    """
    form = dict(await request.form())
    # Taken out first, so nothing below — the error path included — can hand it back.
    password = form.pop("password", "") or ""
    enrolling = form.get("action") == "enrol"
    cfg = config.load()
    servers = cfg.get("servers", [])
    original = form.get("original_name") or None

    try:
        entry = validate.server(form, {s["name"] for s in servers}, original_name=original,
                                schemes=validate.link_schemes(cfg))
        link = validate.guest_link(form, servers, entry["name"], original_name=original)
    except validate.ValidationError as e:
        # `submitted` carries the typed values back into the form; `original` alone would
        # re-render it from storage and quietly discard the edit being reported on.
        return _server_form(request, errors=e.errors, submitted=form, status_code=400)

    if original:
        previous = next((s for s in servers if s["name"] == original), None)
        if previous:
            _carry_hand_written(entry, previous)
        servers = [entry if s["name"] == original else s for s in servers]
        if entry["name"] != original:
            _rename_references(servers, original, entry["name"])
            for project in cfg.get("containers") or []:
                if project.get("server") == original:
                    project["server"] = entry["name"]
            membership.rename(cfg, original, entry["name"])
    else:
        servers.append(entry)

    # One box for both kinds of machine: on a standalone one `validate.server` wrote it as
    # `on_demand`; on a guest it belongs to the hypervisor's `manages_vms` entry.
    _relink_guest(servers, entry["name"], link, on_demand=bool(form.get("on_demand")))

    cfg["servers"] = servers
    config.save(cfg)
    fleet_status.invalidate()  # the dashboard must not show a stale probe for a changed address
    if not enrolling:
        return _done(request)
    if not config.has_ssh(entry):
        return _server_form(request, edit=entry["name"], status_code=400,
                            enrol_error=_("{name} is watched only — there is nothing to enrol.", name=entry["name"]))

    result, error = await asyncio.to_thread(
        _enrol, entry, password, form.get("grant_sudo") in ("on", "true", "1"))
    del password
    # Rendered rather than redirected: the outcome is the whole point of the request, and a
    # redirect would have to carry it in the URL, where it would survive a refresh and a share.
    return _server_form(request, edit=entry["name"], enrol_result=result, enrol_error=error,
                        status_code=400 if error else 200)


def _enrol(server: dict, password: str, grant_sudo: bool) -> tuple[str | None, str | None]:
    """Install Timar's key with the password, or — with none — check the key already works.

    In a thread from the handler: paramiko blocks, and a slow host would otherwise freeze the
    scheduler and every other tab for the length of the handshake.
    """
    try:
        if not password:
            return _("Key checked: {check}", check=enroll_module.verify(server)), None
        outcome = enroll_module.enroll(server, password, grant_sudo=grant_sudo)
        # Proved with the key alone, not with the password connection that just succeeded: the
        # password working says nothing about whether the key will be accepted, and the key is
        # what every later run depends on.
        return _("{outcome} — verified: {check}", outcome=outcome.describe(),
                 check=enroll_module.verify(server)), None
    except enroll_module.EnrollError as e:
        return None, str(e)


@router.post("/servers/{name}/delete")
async def delete_server(request: Request, name: str):
    cfg = config.load()
    servers = cfg.get("servers", [])

    # A hypervisor that manages guests is removed along with the relationship, not silently
    # leaving guests that nothing will ever start.
    removed = next((s for s in servers if s["name"] == name), None)
    cfg["servers"] = [s for s in servers if s["name"] != name]
    membership.forget(cfg, name)
    if removed:
        for host in cfg["servers"]:
            if guests := host.get("manages_vms"):
                host["manages_vms"] = [g for g in guests if g["server_name"] != name]
                if not host["manages_vms"]:
                    del host["manages_vms"]

    config.save(cfg)
    fleet_status.invalidate()
    return _done(request)


@router.post("/llm")
async def save_llm(request: Request):
    form = dict(await request.form())
    cfg = config.load()
    try:
        entry = validate.llm(form, cfg.get("llm"))
    except validate.ValidationError as e:
        return _view(request, errors=e.errors, status_code=400)

    if entry is None:
        cfg.pop("llm", None)
    else:
        cfg["llm"] = entry
    config.save(cfg)
    return _redirect("saved")


@router.post("/llm/test", response_class=HTMLResponse)
async def test_llm(request: Request):
    """Prove the model answers, now, from the settings page.

    A model connection that is only exercised by the nightly sweep is one you discover is
    misconfigured on the morning the report did not arrive.
    """
    cfg = config.load()
    try:
        llm_cfg = llm_module.LLMConfig.from_dict(cfg.get("llm"))
        if llm_cfg is None:
            return HTMLResponse(f'<span class="error">{_escape(_("Save a model connection first."))}</span>')
        reply = llm_module.complete(
            llm_cfg, "You are a connection test. Answer with a single word.", "Reply with: ok"
        )
    except llm_module.LLMError as e:
        return HTMLResponse(f'<span class="error">{_escape(str(e))}</span>')
    return HTMLResponse(f'<span class="ok">{_escape(_("Model replied: {reply}", reply=reply[:80] or _("(empty)")))}</span>')


@router.post("/llm/models", response_class=HTMLResponse)
async def list_llm_models(request: Request):
    """A picker of the provider's models that fills the model field.

    A `<select>` beside the field rather than a `<datalist>` on it: browsers filter a datalist by
    what the field already holds, so with a model saved the "list" showed only its near
    namesakes — three of thirteen. The field itself stays free text, so a model the provider does
    not advertise — a local Ollama tag, one released after this list was fetched — can still be
    typed. The list is a shortcut, not a whitelist. The select has no name: it is never submitted.
    """
    cfg = config.load()
    llm_cfg = llm_module.LLMConfig.from_dict(cfg.get("llm"))
    if llm_cfg is None:
        return HTMLResponse(f'<span class="error">{_escape(_("Save a provider first."))}</span>')
    try:
        models = llm_module.list_models(llm_cfg)
    except llm_module.LLMError as e:
        return HTMLResponse(f'<span class="error">{_escape(str(e))}</span>')
    if not models:
        return HTMLResponse(f'<span class="error">{_escape(_("The provider listed no models."))}</span>')

    hint = _("{n} models — pick one to put it in the model field.", n=len(models))
    options = "".join(f'<option value="{_escape(m)}">{_escape(m)}</option>' for m in models)
    return HTMLResponse(
        f'<select class="model-pick" aria-label="{_escape(_("Model"))}"'
        ' onchange="if (this.value) this.form.model.value = this.value">'
        f'<option value="" selected disabled>{_escape(hint)}</option>{options}</select>'
    )


@router.post("/telegram")
async def save_telegram(request: Request):
    form = dict(await request.form())
    cfg = config.load()
    try:
        entry = validate.telegram(form, cfg.get("telegram"))
    except validate.ValidationError as e:
        return _view(request, errors=e.errors, status_code=400)

    if entry is None:
        cfg.pop("telegram", None)
    else:
        cfg["telegram"] = entry
    config.save(cfg)
    return _redirect("saved")


@router.post("/telegram/test", response_class=HTMLResponse)
async def test_telegram():
    cfg = config.load()
    telegram_cfg = cfg.get("telegram") or {}
    try:
        notify.send_test(telegram_cfg.get("token", ""), telegram_cfg.get("chat_id", ""))
    except notify.NotifyError as e:
        return HTMLResponse(f'<span class="error">{_escape(str(e))}</span>')
    return HTMLResponse(f'<span class="ok">{_escape(_("Sent — check your chat."))}</span>')


def _version_panel(request: Request, *, error: str | None = None):
    """What the version line says after the number: a newer release, an upgrade under way.

    A fragment loaded after the page, so a slow or unreachable GitHub delays only this line.
    While an upgrade is under way it polls itself: the requests that fail while the container
    is replaced are simply retried, and the first answer from the new one says it is done.
    """
    cfg = config.load()
    upgrade = release.upgrade_status(cfg)
    latest = None
    if not (upgrade and upgrade["state"] == "pending") and release.check_enabled(cfg):
        latest = release.latest()
    running = release.current_version()
    return TEMPLATES.TemplateResponse(request, "_version.html", {
        "running": running,
        "latest": latest,
        "newer": bool(latest and release.is_newer(latest["version"], running)),
        "check_enabled": release.check_enabled(cfg),
        "upgrade": upgrade,
        "error": error,
    })


@router.get("/version", response_class=HTMLResponse)
async def version(request: Request):
    return await asyncio.to_thread(_version_panel, request)


@router.post("/upgrade", response_class=HTMLResponse)
async def upgrade(request: Request):
    form = await request.form()
    target = str(form.get("version", ""))
    busy = [jobs.TITLES.get(name, name) for name in jobs.JOBS if scheduler.is_running(name)]
    if busy:
        message = _("Wait for {jobs} to finish — the upgrade restarts timar.",
                    jobs=", ".join(_(b) for b in busy))
        return await asyncio.to_thread(_version_panel, request, error=message)
    try:
        await asyncio.to_thread(release.start_upgrade, config.load(), target)
    except release.UpgradeError as e:
        return await asyncio.to_thread(_version_panel, request, error=str(e))
    except Exception as e:
        message = _("Could not start the upgrade: {error}", error=str(e))
        return await asyncio.to_thread(_version_panel, request, error=message)
    return await asyncio.to_thread(_version_panel, request)


def _job_form(request: Request, name: str, *, errors: list[str] | None = None,
              submitted: dict | None = None, status_code: int = 200):
    """One job's schedule — and the log sweep's own settings — in the reports page's dialog.

    A rejected save comes back with what was typed rather than what is stored.
    """
    if name not in jobs.JOBS:
        raise HTTPException(404)
    cfg = config.load()
    schedule = (cfg.get("schedules") or {}).get(name, {})
    log_check = cfg.get("log_check", {})
    if submitted is not None:
        schedule = {"enabled": submitted.get(f"{name}_enabled") in ("on", "true", "1"),
                    "kind": submitted.get(f"{name}_kind"), "at": submitted.get(f"{name}_at"),
                    "day": submitted.get(f"{name}_day"),
                    "every_hours": submitted.get(f"{name}_every_hours")}
        log_check = {"journal_hours": submitted.get("journal_hours"),
                     "disk_threshold": submitted.get("disk_threshold")}
    job = {"name": name, "title": _(jobs.TITLES[name])}
    return _panel(request, "_job_form.html", {
        **_hosts_context(cfg, name),
        "job": job, "schedules": {name: schedule}, "log_check": log_check,
        "days": list(_DAYS), "kinds": list(_KINDS), "errors": errors or [],
    }, title=_("Edit {name}", name=job["title"]), status_code=status_code, section="reports")


def _hosts_context(cfg: dict, name: str) -> dict:
    runs, left, unenrolled = membership.lists(cfg, name)
    return {"job": {"name": name, "title": _(jobs.TITLES[name])},
            "runs_on": runs, "left_out": left, "not_enrolled": unenrolled}


@router.post("/jobs/{name}/hosts/{server}/{action}", response_class=HTMLResponse)
async def job_host(request: Request, name: str, server: str, action: str):
    """Leave a server out of a job, or take it back in. Saved at once; answers with the lists.

    Only the lists are re-rendered, so a schedule being edited in the same dialog is not lost.
    """
    if name not in jobs.JOBS or action not in ("leave", "join"):
        raise HTTPException(404)
    cfg = config.load()
    if not any(s["name"] == server for s in cfg.get("servers", [])):
        raise HTTPException(404)
    membership.set_excluded(cfg, name, server, leave_out=action == "leave")
    config.save(cfg)
    return TEMPLATES.TemplateResponse(request, "_job_hosts.html", _hosts_context(cfg, name))


@router.get("/jobs/{name}/edit", response_class=HTMLResponse)
async def edit_job(request: Request, name: str):
    return _job_form(request, name)


@router.post("/jobs/{name}")
async def save_job(request: Request, name: str):
    """This job's schedule — and, for the sweep, its settings — validated together."""
    if name not in jobs.JOBS:
        raise HTTPException(404)
    form = dict(await request.form())
    cfg = config.load()
    errors: list[str] = []
    log_check = None
    if name == jobs.LOG_SWEEP:
        try:
            log_check = validate.log_check(form)
        except validate.ValidationError as e:
            errors += e.errors
    try:
        schedule = validate.schedules(form, [name])[name]
    except validate.ValidationError as e:
        errors += e.errors
    if errors:
        return _job_form(request, name, errors=errors, submitted=form, status_code=400)
    if log_check is not None:
        cfg["log_check"] = log_check
    cfg["schedules"] = {**(cfg.get("schedules") or {}), name: schedule}
    config.save(cfg)
    # The running loop re-reads config on its next tick, so no restart is needed -- but the
    # stored next_run is now wrong until that happens, and a dashboard showing a next run that
    # no longer matches the schedule is exactly the kind of thing that erodes trust in it.
    state.set_next_run(name, None)
    return _done(request, "/reports")


@router.get("/servers/{name}/enroll")
async def enroll_form(name: str):
    """Enrolment is the form's SSH section now; old links and bookmarks land there."""
    server = _find_server(name)
    return RedirectResponse(f"/settings/servers/{quote(server['name'])}/edit#ssh-access",
                            status_code=SEE_OTHER)


def _find_server(name: str) -> dict:
    server = next((s for s in config.load().get("servers", []) if s["name"] == name), None)
    if server is None:
        raise HTTPException(404)
    return server


# -- containers ----------------------------------------------------------------------------
# The same shape as the server routes above: one form for add and edit, opened in the containers
# page's dialog or as a page of its own, and a removal that only forgets the entry.

def _container_form(request: Request, *, edit: str | None = None, submitted: dict | None = None,
                    errors: list[str] | None = None, status_code: int = 200):
    cfg = config.load()
    entries = cfg.get("containers") or []
    editing = (submitted or {}).get("original_name") or edit
    if editing and not any(c["name"] == editing for c in entries):
        editing = None
    if submitted is not None:
        values = dict(submitted)
    else:
        values = next((dict(c) for c in entries if c["name"] == editing), {})
    hosts = [s for s in cfg.get("servers", [])
             if get_platform(s.get("platform")).supports_docker]
    return _panel(request, "_container_form.html", {
        "editing": editing, "values": values, "hosts": hosts, "errors": errors or [],
        "default_update_timeout": updater.DEFAULT_UPDATE_TIMEOUT,
        "min_update_timeout": validate.MIN_UPDATE_TIMEOUT,
        "max_update_timeout": validate.MAX_UPDATE_TIMEOUT,
    }, title=_("Edit {name}", name=editing) if editing else _("Add a container"),
       status_code=status_code, section="containers")


def _find_container(name: str) -> dict:
    entry = next((c for c in config.load().get("containers") or [] if c["name"] == name), None)
    if entry is None:
        raise HTTPException(404)
    return entry


@router.get("/containers/new", response_class=HTMLResponse)
async def new_container(request: Request):
    return _container_form(request)


@router.get("/containers/{name}/edit", response_class=HTMLResponse)
async def edit_container(request: Request, name: str):
    _find_container(name)
    return _container_form(request, edit=name)


@router.post("/containers")
async def save_container(request: Request):
    form = dict(await request.form())
    cfg = config.load()
    entries = cfg.get("containers") or []
    original = form.get("original_name") or None
    try:
        entry = validate.container(form, {c["name"] for c in entries}, cfg.get("servers", []),
                                   original_name=original)
    except validate.ValidationError as e:
        return _container_form(request, errors=e.errors, submitted=form, status_code=400)
    if original:
        previous = next((c for c in entries if c["name"] == original), None)
        if previous:
            # Keys the form does not own — later releases' update settings, hand-written ones —
            # are carried across, as for servers.
            for key, value in previous.items():
                if key not in validate.CONTAINER_FIELDS:
                    entry.setdefault(key, value)
        entries = [entry if c["name"] == original else c for c in entries]
    else:
        entries.append(entry)
    cfg["containers"] = entries
    config.save(cfg)
    container_status.invalidate()
    return _done(request, "/containers")


@router.post("/containers/{name}/delete")
async def delete_container(request: Request, name: str):
    """Forget the entry. The project on the host is not touched."""
    cfg = config.load()
    cfg["containers"] = [c for c in cfg.get("containers") or [] if c["name"] != name]
    config.save(cfg)
    container_status.invalidate()
    return _done(request, "/containers")


def _discover_panel(request: Request, server_name: str, *, errors: list[str] | None = None,
                    submitted: dict | None = None, picked: list[str] | None = None,
                    status_code: int = 200):
    """The projects on one host that are not registered yet, to tick and add."""
    cfg = config.load()
    server = next((x for x in cfg.get("servers", []) if x["name"] == server_name), None)
    if server is None:
        raise HTTPException(404)
    registered = {(c.get("server"), c.get("path", "").rstrip("/")) for c in cfg.get("containers") or []}
    found, error = [], None
    try:
        found = [f for f in container_status.discover(server) if (server_name, f.path) not in registered]
    except container_status.ContainerError as e:
        error = str(e)
    return _panel(request, "_container_discover.html", {
        "server": server_name, "found": found, "error": error, "errors": errors or [],
        "submitted": submitted or {}, "picked": picked or [],
    }, title=_("Find on a host"), status_code=status_code, section="containers")


@router.get("/containers/discover", response_class=HTMLResponse)
async def discover_containers(request: Request, server: str = ""):
    return await asyncio.to_thread(_discover_panel, request, server)


@router.post("/containers/import")
async def import_containers(request: Request):
    """Add the ticked projects — all of them or, if any row is wrong, none.

    All-or-nothing so a rejected list can be fixed and sent again as it is, without working out
    which half of it already went in.
    """
    form = await request.form()
    server_name = form.get("server") or ""
    cfg = config.load()
    entries = cfg.get("containers") or []
    names = {c["name"] for c in entries}
    added, errors = [], []
    for i in form.getlist("pick"):
        row = {"name": form.get(f"name_{i}", ""), "server": server_name,
               "path": form.get(f"path_{i}", ""), "web_url": form.get(f"web_{i}", "")}
        try:
            entry = validate.container(row, names, cfg.get("servers", []))
        except validate.ValidationError as e:
            errors += [f"{row['name'] or row['path']}: {message}" for message in e.errors]
            continue
        names.add(entry["name"])
        added.append(entry)
    if not added and not errors:
        errors.append(_("Tick at least one project."))
    if errors:
        return await asyncio.to_thread(_discover_panel, request, server_name, errors=errors,
                                       submitted=dict(form), picked=form.getlist("pick"),
                                       status_code=400)
    cfg["containers"] = entries + added
    config.save(cfg)
    container_status.invalidate()
    return _done(request, "/containers")
