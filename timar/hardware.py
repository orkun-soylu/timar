"""The hardware inventory: Markdown files the operator keeps about the machines themselves.

What the fleet table cannot show — the board, the cards, which MAC moved to which box, the cooler
that does not fit the case. It lives in `hardware/` in the data volume, one `.md` file per
document, and the files are the store: no index, no database. Mounting that directory from
somewhere else (a git checkout, a share) is how an installation versions it or edits it with its
own tools, which is also why a save rewrites a file atomically but never touches anything else in
the directory.

Saves carry the digest of the text they started from. Two people — or a person and a script —
editing the same file is the normal case for a document like this, and the later save must not
quietly erase the earlier one.

**Raw HTML is not rendered.** The page that shows these files belongs to an application holding
an SSH key to every machine in the fleet; an uploaded file must not be able to run script in that
page. HTML in the source shows as text, and a link or image may only point at the web or at
another page of this site.
"""
from __future__ import annotations

import hashlib
import html as html_module
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import markdown
from markdown.treeprocessors import Treeprocessor

from . import config
from .i18n import gettext as _

DIR = "hardware"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BYTES = 1024 * 1024
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
_SAFE_LINK = re.compile(r"^(https?:|mailto:|#|/(?!/)|[^:/?#]+(?:[/?#]|$))", re.I)


class HardwareError(ValueError):
    pass


class Conflict(HardwareError):
    """The file changed since the editor opened it."""


def directory() -> Path:
    return config.path(DIR)


def slugify(name: str) -> str:
    """A file name as a page address: `Inventory.md` → `inventory`, `Rack 2` → `rack-2`."""
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:64]


def _file(slug: str) -> Path:
    if not SLUG.fullmatch(slug):
        raise HardwareError(_("Not a document name: lower-case letters, digits and hyphens."))
    return directory() / f"{slug}.md"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_front_matter(text: str) -> tuple[dict, str]:
    """`key: value` lines between `---` fences, if the file starts with them, and the rest.

    Read loosely, a line at a time: these are notes, and a fence written by hand must not make
    the page fail to open. Only `title` and `order` are used.
    """
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip().strip("'\"")
    return meta, text[match.end():]


def _title(slug: str, text: str) -> str:
    meta, body = split_front_matter(text)
    if meta.get("title"):
        return meta["title"]
    heading = re.search(r"^# +(.+?)\s*#*\s*$", body, re.M)
    return heading.group(1) if heading else slug


def _order(text: str) -> int:
    """`order:` in the front matter, for documents that should come first; unset ones go last."""
    value = split_front_matter(text)[0].get("order", "")
    return int(value) if value.lstrip("-").isdigit() else 1_000_000


def listing() -> list[dict]:
    """Every document, by `order:` and then by title — the order of the tabs."""
    d = directory()
    if not d.is_dir():
        return []
    docs = []
    for p in d.glob("*.md"):
        if not SLUG.fullmatch(p.stem):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        docs.append({"slug": p.stem, "title": _title(p.stem, text),
                     "modified": _modified(p), "order": _order(text)})
    return sorted(docs, key=lambda doc: (doc["order"], doc["title"].lower()))


def _modified(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def get(slug: str) -> dict | None:
    p = _file(slug)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    return {"slug": slug, "title": _title(slug, text), "text": text, "digest": digest(text),
            "modified": _modified(p)}


def exists(slug: str) -> bool:
    return _file(slug).is_file()


def stored_name(slug: str) -> str | None:
    """The name of the file on disk that `slug` refers to, or None.

    For building addresses: the caller gets the directory's own spelling, never the request
    value — even one that was checked first.
    """
    if not SLUG.fullmatch(slug):
        return None
    return next((p.stem for p in directory().glob("*.md") if p.stem == slug), None)


def save(slug: str, text: str, base_digest: str | None) -> None:
    """Write a document, refusing if it changed since `base_digest` was read.

    `base_digest` is None for a new document, which then must not exist yet. Line endings are
    normalised: a browser's textarea sends `\\r\\n`, and a file edited from both sides would
    otherwise flip-flop between the two on every save.
    """
    p = _file(slug)
    text = text.replace("\r\n", "\n")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise HardwareError(_("Too large: a document can be at most 1 MB."))
    current = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    if base_digest is None and current is not None:
        raise Conflict(_("A document with this name already exists."))
    if base_digest is not None and (current is None or digest(current) != base_digest):
        raise Conflict("The document changed while you were editing it.")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def delete(slug: str) -> None:
    _file(slug).unlink(missing_ok=True)


class _SafeLinks(Treeprocessor):
    """Drop any link or image address that is not the web, mail, or a page of this site."""

    def run(self, root: ElementTree.Element) -> None:
        for element in root.iter():
            for attribute in ("href", "src"):
                value = element.get(attribute)
                # Checked as the browser will read it: the serialiser writes entities through,
                # so `&#106;avascript:` would otherwise pass as a relative link.
                if value is not None and not _SAFE_LINK.match(
                        html_module.unescape(value).strip()):
                    element.set(attribute, "#")


def render(text: str) -> str:
    _, body = split_front_matter(text)
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "toc"])
    # Raw HTML, block and inline, is left as escaped text instead of passed through.
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    md.treeprocessors.register(_SafeLinks(md), "safe_links", 5)
    html = md.convert(body)
    # A wide table scrolls in its own box rather than pushing the page sideways.
    return html.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>")
