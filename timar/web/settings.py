"""Settings: servers, log sweep defaults, the model connection, and notifications.

Every route here rewrites `config.yaml` through `config.save()`, which replaces the file
atomically — the scheduler may be reading it at the same moment.

**Secrets are never sent to the browser.** The forms show whether a key is stored, not what it
is, and an empty key field means "leave it as it was". That has to be the rule rather than
"empty means delete", because a blank box is the *normal* state of the field on every visit —
treating it as a deletion would wipe the credential on any unrelated edit to the same form.
"""
from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import (config, enroll as enroll_module, jobs, keys, llm as llm_module, notify,
                state, status as fleet_status, updater, validate, wol)
from ..platforms import PLATFORMS, get as get_platform
from ..schedule import DAYS as _DAYS, KINDS as _KINDS
from .auth import require_operator

# Every route in this file is behind the session guard. Declared once on the router rather than
# per-route: a new settings endpoint should be protected because it is a settings endpoint, not
# because whoever added it remembered a decorator.
router = APIRouter(prefix="/settings", dependencies=[Depends(require_operator)])
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SEE_OTHER = 303


def _escape(text: str) -> str:
    """These strings carry third-party error bodies straight into the page."""
    return html.escape(text)


def _view(request: Request, *, errors: list[str] | None = None, notice: str | None = None,
          edit: str | None = None, status_code: int = 200):
    cfg = config.load()
    llm_cfg = cfg.get("llm") or {}
    telegram_cfg = cfg.get("telegram") or {}
    return TEMPLATES.TemplateResponse(request, "settings.html", {
        "servers": cfg.get("servers", []),
        "on_demand": config.on_demand(cfg.get("servers", [])),
        "guest_of": {
            guest["server_name"]: {"hypervisor": host["name"], "vm_id": guest["vm_id"]}
            for host in cfg.get("servers", [])
            for guest in host.get("manages_vms", [])
        },
        "log_check": cfg.get("log_check", {}),
        "llm": {k: v for k, v in llm_cfg.items() if k != "api_key"},
        "llm_has_key": bool(llm_cfg.get("api_key")),
        "telegram_chat_id": telegram_cfg.get("chat_id", ""),
        "telegram_has_token": bool(telegram_cfg.get("token")),
        "platforms": list(PLATFORMS),
        "default_update_timeout": updater.DEFAULT_UPDATE_TIMEOUT,
        "min_update_timeout": validate.MIN_UPDATE_TIMEOUT,
        "max_update_timeout": validate.MAX_UPDATE_TIMEOUT,
        "providers": llm_module.PROVIDERS,
        "schedules": cfg.get("schedules") or {},
        "jobs": [{"name": n, "title": jobs.TITLES[n]} for n in jobs.JOBS],
        "days": list(_DAYS),
        "kinds": list(_KINDS),
        "errors": errors or [],
        "notice": notice,
        "edit": edit,
    }, status_code=status_code)


def _redirect(notice: str | None = None):
    url = f"/settings?notice={notice}" if notice else "/settings"
    return RedirectResponse(url, status_code=SEE_OTHER)


@router.get("", response_class=HTMLResponse)
async def page(request: Request, notice: str | None = None, edit: str | None = None):
    return _view(request, notice=notice, edit=edit)


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


def _relink_guest(servers: list[dict], name: str, link: tuple[str, int] | None) -> None:
    """Make `name` a guest of exactly the hypervisor in `link`, or of none at all."""
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
            server.setdefault("manages_vms", []).append(
                {"vm_id": vm_id, "server_name": name})


@router.post("/servers")
async def save_server(request: Request):
    """Add a server, or replace one when `original_name` is present.

    Add and edit share a handler because they share every rule; splitting them is how the two
    paths drift until one of them stops validating something.
    """
    form = dict(await request.form())
    cfg = config.load()
    servers = cfg.get("servers", [])
    original = form.get("original_name") or None

    try:
        entry = validate.server(form, {s["name"] for s in servers}, original_name=original)
        link = validate.guest_link(form, servers, entry["name"], original_name=original)
    except validate.ValidationError as e:
        return _view(request, errors=e.errors, edit=original, status_code=400)

    if original:
        servers = [entry if s["name"] == original else s for s in servers]
        if entry["name"] != original:
            _rename_references(servers, original, entry["name"])
    else:
        servers.append(entry)

    _relink_guest(servers, entry["name"], link)

    cfg["servers"] = servers
    config.save(cfg)
    fleet_status.invalidate()  # the dashboard must not show a stale probe for a changed address
    return _redirect("saved")


@router.post("/servers/{name}/delete")
async def delete_server(name: str):
    cfg = config.load()
    servers = cfg.get("servers", [])

    # A hypervisor that manages guests is removed along with the relationship, not silently
    # leaving guests that nothing will ever start.
    removed = next((s for s in servers if s["name"] == name), None)
    cfg["servers"] = [s for s in servers if s["name"] != name]
    if removed:
        for host in cfg["servers"]:
            if guests := host.get("manages_vms"):
                host["manages_vms"] = [g for g in guests if g["server_name"] != name]
                if not host["manages_vms"]:
                    del host["manages_vms"]

    config.save(cfg)
    fleet_status.invalidate()
    return _redirect("deleted")


@router.post("/log-check")
async def save_log_check(request: Request):
    form = dict(await request.form())
    cfg = config.load()
    try:
        cfg["log_check"] = validate.log_check(form)
    except validate.ValidationError as e:
        return _view(request, errors=e.errors, status_code=400)
    config.save(cfg)
    return _redirect("saved")


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
            return HTMLResponse('<span class="error">Save a model connection first.</span>')
        reply = llm_module.complete(
            llm_cfg, "You are a connection test. Answer with a single word.", "Reply with: ok"
        )
    except llm_module.LLMError as e:
        return HTMLResponse(f'<span class="error">{_escape(str(e))}</span>')
    return HTMLResponse(f'<span class="ok">Model replied: {_escape(reply[:80]) or "(empty)"}</span>')


@router.post("/llm/models", response_class=HTMLResponse)
async def list_llm_models(request: Request):
    """Fill the model field's suggestion list from the provider.

    Returns a `<datalist>` rather than a `<select>` on purpose: the field stays free text, so a
    model the provider does not advertise — a local Ollama tag, one released after this list was
    fetched — can still be typed. The list is a shortcut, not a whitelist.
    """
    cfg = config.load()
    llm_cfg = llm_module.LLMConfig.from_dict(cfg.get("llm"))
    if llm_cfg is None:
        return HTMLResponse('<span class="error">Save a provider first.</span>')
    try:
        models = llm_module.list_models(llm_cfg)
    except llm_module.LLMError as e:
        return HTMLResponse(f'<span class="error">{_escape(str(e))}</span>')
    if not models:
        return HTMLResponse('<span class="error">The provider listed no models.</span>')

    options = "".join(f'<option value="{_escape(m)}">' for m in models)
    return HTMLResponse(
        f'<datalist id="model-options">{options}</datalist>'
        f'<span class="ok">{len(models)} models — click the model field.</span>'
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
    return HTMLResponse('<span class="ok">Sent — check your chat.</span>')


@router.post("/schedules")
async def save_schedules(request: Request):
    form = dict(await request.form())
    cfg = config.load()
    try:
        cfg["schedules"] = validate.schedules(form, jobs.JOBS)
    except validate.ValidationError as e:
        return _view(request, errors=e.errors, status_code=400)
    config.save(cfg)
    # The running loops re-read config on their next tick, so no restart is needed -- but the
    # stored next_run is now wrong until that happens, and a dashboard showing a next run that
    # no longer matches the schedule is exactly the kind of thing that erodes trust in it.
    for name in jobs.JOBS:
        state.set_next_run(name, None)
    return _redirect("saved")


@router.get("/servers/{name}/enroll", response_class=HTMLResponse)
async def enroll_form(request: Request, name: str):
    server = _find_server(name)
    platform = get_platform(server.get("platform"))
    return TEMPLATES.TemplateResponse(request, "enroll.html", {
        "server": server,
        "platform": platform,
        "fingerprint": keys.fingerprint(),
        "public_key": keys.public_key(),
        "can_sudo": platform.supports_sudo and server["user"] != "root",
        "error": None,
        "result": None,
    })


@router.post("/servers/{name}/enroll", response_class=HTMLResponse)
async def enroll_submit(request: Request, name: str):
    """Install Timar's key on a host, using the operator's password once.

    The password lives for the length of this request: it goes to the SSH channel and nowhere
    else. It is never written to the config, the state file, or the log, and it is never sent
    back to the page -- including on the error path, where re-rendering the form with the field
    refilled would put it in the browser's history and any proxy in between.
    """
    form = dict(await request.form())
    server = _find_server(name)
    platform = get_platform(server.get("platform"))
    password = form.get("password") or ""
    wants_sudo = form.get("grant_sudo") in ("on", "true", "1")

    error = None
    result = None
    if not password:
        error = "The SSH password is required."
    else:
        try:
            outcome = enroll_module.enroll(server, password, grant_sudo=wants_sudo)
            # Proved with the key alone, not with the password connection that just succeeded:
            # the password working says nothing about whether the key will be accepted, and the
            # key is what every later run depends on.
            result = f"{outcome.describe()} — verified: {enroll_module.verify(server)}"
        except enroll_module.EnrollError as e:
            error = str(e)
    del password

    return TEMPLATES.TemplateResponse(request, "enroll.html", {
        "server": server,
        "platform": platform,
        "fingerprint": keys.fingerprint(),
        "public_key": keys.public_key(),
        "can_sudo": platform.supports_sudo and server["user"] != "root",
        "error": error,
        "result": result,
    }, status_code=400 if error else 200)


@router.post("/servers/{name}/verify", response_class=HTMLResponse)
async def verify_server(name: str):
    server = _find_server(name)
    try:
        return HTMLResponse(f'<span class="ok">{_escape(enroll_module.verify(server))}</span>')
    except enroll_module.EnrollError as e:
        return HTMLResponse(f'<span class="error">{_escape(str(e))}</span>')


def _find_server(name: str) -> dict:
    server = next((s for s in config.load().get("servers", []) if s["name"] == name), None)
    if server is None:
        raise HTTPException(404)
    return server


@router.post("/servers/{name}/wake", response_class=HTMLResponse)
async def wake_server(name: str):
    """Send a magic packet now.

    Waking is the one Timar operation with no feedback of its own — the packet is fire and
    forget, and a machine that stays dark could mean a wrong MAC, a packet that never left the
    host, or Wake-on-LAN simply disabled in its firmware. Being able to press the button and
    watch the dashboard is how an operator tells those apart.
    """
    cfg = config.load()
    server = _find_server(name)
    try:
        wol.wake(server, {s["name"]: s for s in cfg.get("servers", [])})
    except wol.WolError as e:
        return HTMLResponse(f'<span class="error">{_escape(str(e))}</span>')
    fleet_status.invalidate()  # the machine is about to change state; a cached probe would lie
    via = f" via {server['wol_relay']}" if server.get("wol_relay") else ""
    return HTMLResponse(
        f'<span class="ok">Magic packet sent{_escape(via)} — watch the dashboard.</span>')
