"""Static file URLs that change when the file does: `/static/logos.svg?v=<hash>`.

The files are served without `Cache-Control`, so a browser keeps them by its own heuristic — for a
while after a release that changed them. Found when two logos added to the sprite stayed blank:
the page asked for `#tplink` in the sprite the browser already had, which had no such symbol, and
an SVG `<use>` that misses draws nothing and reports nothing. A content hash in the URL makes a
changed file a new URL; an unchanged one keeps its cache.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

STATIC = Path(__file__).parent / "static"


@lru_cache(maxsize=None)
def static_url(name: str) -> str:
    """Read once per process: the files ship in the image and do not change under it."""
    digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={digest}"


def install(env) -> None:
    env.globals["static_url"] = static_url
