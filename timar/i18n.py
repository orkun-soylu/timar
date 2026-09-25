"""Interface languages.

The English text is the message id: templates and code read as they always did, and a string
nobody has translated yet falls back to the English it already is rather than to a key name.
Catalogs are flat JSON in `locales/`, one file per language, English → translation.

The language is per request, held in a context variable that the web middleware sets. Anything
that runs outside a request — the scheduler, reports, Telegram — sees the default and stays in
English: a report is read later, by whoever happens to read it, and a stored summary must not
depend on the language of the browser that happened to trigger the run.

Placeholders are `str.format` fields (`{name}`), so a translation can move them to wherever its
grammar wants them.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from functools import cache
from pathlib import Path

from markupsafe import Markup

DEFAULT = "en"

# Each language named in itself: someone who cannot read the current interface has to be able
# to find their own language in the list.
LANGUAGES = {
    "en": "English",
    "tr": "Türkçe",
    "de": "Deutsch",
    "fr": "Français",
    "ru": "Русский",
    "ja": "日本語",
    "zh": "中文",
}

COOKIE = "timar_lang"
LOCALES = Path(__file__).parent / "locales"

_current: ContextVar[str] = ContextVar("timar_lang", default=DEFAULT)


@cache
def _catalog(lang: str) -> dict[str, str]:
    if lang == DEFAULT:
        return {}
    path = LOCALES / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def current() -> str:
    return _current.get()


def activate(lang: str | None) -> None:
    _current.set(lang if lang in LANGUAGES else DEFAULT)


def negotiate(cookie: str | None, accept_language: str | None) -> str:
    """The cookie if it names a language we have, else the browser's best match, else English."""
    if cookie in LANGUAGES:
        return cookie
    ranked = []
    for index, part in enumerate((accept_language or "").split(",")):
        tag, _, params = part.strip().partition(";")
        quality = 1.0
        if params.strip().startswith("q="):
            try:
                quality = float(params.strip()[2:])
            except ValueError:
                continue
        # `zh-CN`, `pt-BR`: only the primary subtag decides. One Chinese catalog, one German.
        primary = tag.strip().lower().split("-")[0]
        if primary in LANGUAGES and quality > 0:
            ranked.append((-quality, index, primary))
    return min(ranked)[2] if ranked else DEFAULT


def gettext(message: str, /, **values) -> str:
    """Translate `message` into the request's language and fill in its placeholders."""
    text = _catalog(current()).get(message) or message
    return text.format(**values) if values else text


def ngettext(singular: str, plural: str, n: int, /, **values) -> str:
    return gettext(singular if n == 1 else plural, n=n, **values)


def _markup(message: str, /, **values) -> Markup:
    """The template flavour: the translation may carry markup, the values it is filled with may not.

    `Markup.format` escapes every argument, so a server name still cannot inject HTML even
    though the sentence around it is trusted.
    """
    text = Markup(_catalog(current()).get(message) or message)
    return text.format(**values) if values else text


def install(env) -> None:
    """Make `_`, the language list and the current language available to a Jinja environment."""
    env.globals.update(_=_markup, current_lang=current, languages=LANGUAGES)
