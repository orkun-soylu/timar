"""A job's one-line summary, as chips: "9 updated, 1 failed, 3 skipped" read at a glance.

The summary stays the stored record — plain English text, written by `jobs` — and this only
draws it. Each "N word" part becomes a chip coloured by what the word means; a count of zero is
dropped, because "0 failed" is a chip that says nothing and still draws the eye. Anything that is
not a count (the "— stopped by the operator" tail) is kept as plain text after them.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Longest first: "not in the sweep" must not be read as a shorter word inside it.
KINDS = {
    "failed": "down", "unreachable": "down",
    "with findings": "warn",
    "updated": "up",
    "skipped": "muted", "asleep": "muted", "not in the sweep": "muted",
}

_PART = re.compile(r"^(\d+) (.+)$")


class Chip(NamedTuple):
    text: str
    kind: str       # up, warn, down, muted; "clear" (the page writes "all clear"); "" plain text


def chips(summary: str | None) -> list[Chip]:
    if not summary:
        return []
    head, dash, tail = summary.partition(" — ")
    out: list[Chip] = []
    for part in head.split(", "):
        m = _PART.match(part.strip())
        if not m or m.group(2) not in KINDS:
            return [Chip(summary, "")]      # not a summary this knows: shown as it is
        if int(m.group(1)):
            out.append(Chip(part.strip(), KINDS[m.group(2)]))
    # Nothing failed, found or updated: say it was clean, in green, before the grey counts.
    # "3 asleep" alone reads as a run with nothing to report — true, but not reassuring.
    if not any(c.kind in ("up", "warn", "down") for c in out):
        out.insert(0, Chip("", "clear"))
    if dash:
        out.append(Chip(tail, ""))
    return out


def install(env) -> None:
    env.globals["outcome_chips"] = chips
