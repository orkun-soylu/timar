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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config, llm as llm_module, notify, status as fleet_status, validate
from ..platforms import PLATFORMS
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
        "log_check": cfg.get("log_check", {}),
        "llm": {k: v for k, v in llm_cfg.items() if k != "api_key"},
        "llm_has_key": bool(llm_cfg.get("api_key")),
        "telegram_chat_id": telegram_cfg.get("chat_id", ""),
        "telegram_has_token": bool(telegram_cfg.get("token")),
        "platforms": list(PLATFORMS),
        "providers": llm_module.PROVIDERS,
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
    except validate.ValidationError as e:
        return _view(request, errors=e.errors, edit=original, status_code=400)

    if original:
        servers = [entry if s["name"] == original else s for s in servers]
    else:
        servers.append(entry)

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
