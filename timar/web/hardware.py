"""The hardware pages: the inventory documents, read as pages and edited as Markdown.

Read first, edit on request: the documents are looked up far more often than they are written,
and a page that opens as a text box invites accidental edits. The "+" on the list takes an
uploaded `.md` file or starts an empty one.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from .. import hardware, i18n
from ..i18n import gettext as _
from . import assets, outcome
from .auth import require_operator

router = APIRouter(prefix="/hardware", dependencies=[Depends(require_operator)])
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
i18n.install(TEMPLATES.env)
outcome.install(TEMPLATES.env)
assets.install(TEMPLATES.env)

SEE_OTHER = 303


def _doc(slug: str) -> dict:
    try:
        doc = hardware.get(slug)
    except hardware.HardwareError:
        doc = None
    if doc is None:
        raise HTTPException(404)
    return doc


def _listing(request: Request, error: str | None = None, name: str = "", status_code: int = 200):
    return TEMPLATES.TemplateResponse(request, "hardware.html", {
        "docs": hardware.listing(), "error": error, "name": name,
    }, status_code=status_code)


def _to(slug: str, suffix: str = "") -> RedirectResponse:
    """Back to a document just written, addressed by the name on disk (see `stored_name`)."""
    name = hardware.stored_name(slug)
    target = f"/hardware/{quote(name)}{suffix}" if name else "/hardware"
    return RedirectResponse(target, status_code=SEE_OTHER)


@router.get("", response_class=HTMLResponse)
async def index(request: Request):
    return _listing(request)


@router.post("")
async def add(request: Request):
    """A new document: the uploaded file's text, or an empty page under the name given.

    The name box wins over the file's own name, so `inventory (2).md` can arrive as `inventory`.
    """
    form = await request.form()
    upload = form.get("file")
    name = str(form.get("name") or "").strip()
    has_file = isinstance(upload, UploadFile) and upload.filename
    slug = hardware.slugify(name or (upload.filename if has_file else ""))
    if not slug:
        return _listing(request, _("Give the document a name, or choose a file."), name, 400)
    if has_file:
        raw = await upload.read(hardware.MAX_BYTES + 1)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _listing(request, _("The file is not UTF-8 text."), name, 400)
    else:
        text = f"# {name}\n"
    try:
        hardware.save(slug, text, None)
    except hardware.HardwareError as e:
        return _listing(request, str(e), name, 400)
    return _to(slug, "" if has_file else "/edit")


@router.get("/{slug}", response_class=HTMLResponse)
async def view(request: Request, slug: str):
    doc = _doc(slug)
    return TEMPLATES.TemplateResponse(request, "hardware_doc.html", {
        "doc": doc, "html": hardware.render(doc["text"]),
    })


def _editor(request: Request, doc: dict, text: str, base: str, error: str | None = None,
            status_code: int = 200):
    return TEMPLATES.TemplateResponse(request, "hardware_edit.html", {
        "doc": doc, "text": text, "base": base, "error": error,
    }, status_code=status_code)


@router.get("/{slug}/edit", response_class=HTMLResponse)
async def edit(request: Request, slug: str):
    doc = _doc(slug)
    return _editor(request, doc, doc["text"], doc["digest"])


@router.post("/{slug}")
async def save(request: Request, slug: str):
    """Save an edit, unless the file changed underneath it.

    On a conflict the editor comes back with the operator's text and the *current* digest: what
    they typed is not lost, and saving again is a deliberate choice to replace the other change.
    """
    doc = _doc(slug)
    form = await request.form()
    text = str(form.get("text") or "")
    try:
        hardware.save(slug, text, str(form.get("base") or ""))
    except hardware.Conflict:
        return _editor(request, doc, text, doc["digest"], _(
            "This document changed while you were editing it. Your text is below; saving again "
            "replaces the other change — open the page in another tab to compare first."), 409)
    except hardware.HardwareError as e:
        return _editor(request, doc, text, str(form.get("base") or ""), str(e), 400)
    return _to(slug)


@router.post("/{slug}/delete")
async def remove(slug: str):
    _doc(slug)
    hardware.delete(slug)
    return RedirectResponse("/hardware", status_code=SEE_OTHER)
