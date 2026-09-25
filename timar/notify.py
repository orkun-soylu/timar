"""Telegram delivery.

A push channel matters more here than in most tools: the machines Timar watches are the ones
nobody is looking at. A report that only exists in a web page is a report nobody reads until
they already suspect something, which is exactly the wrong time.

Notification is optional — everything works without it, the findings just stay in the UI.
"""
from __future__ import annotations

import html
import logging
import re

import httpx

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
# Telegram rejects anything longer outright, so a long report is split rather than lost.
MAX_MESSAGE = 4096
TIMEOUT = 20.0


class NotifyError(RuntimeError):
    pass


def escape(text: str) -> str:
    """Escape for Telegram's HTML parse mode.

    Log lines are full of `<`, `>` and `&` — an unescaped angle bracket makes Telegram reject
    the whole message as malformed markup, so the report that mattered most is the one that
    fails to send.
    """
    return html.escape(text, quote=False)


_TAG = re.compile(r"<(/?)([a-zA-Z0-9-]+)[^>]*>")
# Room kept free on every chunk for the closing tags it may need. Reports nest at most a couple
# of short tags (`<b>`, `<i>`, `<pre>`), so this is generous.
_TAG_SLACK = 64


def _track(stack: list[tuple[str, str]], line: str) -> None:
    """Update the stack of open tags with the ones `line` opens and closes."""
    for m in _TAG.finditer(line):
        if m.group(1):
            if stack and stack[-1][0] == m.group(2):
                stack.pop()
        else:
            stack.append((m.group(2), m.group(0)))


def _closing(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _opening(stack: list[tuple[str, str]]) -> str:
    return "".join(tag for _, tag in stack)


def _fit(line: str, room: int) -> str:
    """Truncate an over-long line without leaving half an entity or half a tag at the cut."""
    if len(line) <= room:
        return line
    return re.sub(r"&[^;\s]*$|<[^>]*$", "", line[:room])


def _split(text: str) -> list[str]:
    """Break an over-long message on line boundaries, never mid-line.

    Splitting on a raw character count can cut an HTML tag in half, and Telegram then rejects
    the fragment. Splitting on lines is not enough on its own: a findings report is one `<pre>`
    block, and once it outgrew a single message the first chunk ended inside an unclosed `<pre>`
    and the second began with a stray `</pre>` -- Telegram rejected both ("Can't find end tag
    corresponding to start tag"), and the sweep report was never delivered. So every chunk
    closes the tags still open at its end, and the next one reopens them.
    """
    if len(text) <= MAX_MESSAGE:
        return [text]

    chunks: list[str] = []
    current = ""
    stack: list[tuple[str, str]] = []
    for line in text.split("\n"):
        # A single line longer than the limit still has to go somewhere.
        line = _fit(line, MAX_MESSAGE - len(_opening(stack)) - _TAG_SLACK)
        after = list(stack)
        _track(after, line)
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) + len(_closing(after)) > MAX_MESSAGE:
            chunks.append(current + _closing(stack))
            current = _opening(stack) + line
        else:
            current = candidate
        stack = after
    if current:
        chunks.append(current)
    return chunks


def send(token: str, chat_id: str, text: str) -> None:
    """Deliver a message, raising NotifyError on any failure."""
    if not token or not chat_id:
        raise NotifyError("Telegram is not configured")

    for chunk in _split(text):
        try:
            response = httpx.post(
                f"{API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Telegram's body says which half is wrong -- a bad token and an unknown chat id are
            # different problems with different fixes, and the status code alone conflates them.
            detail = e.response.text[:200].replace("\n", " ")
            raise NotifyError(f"Telegram returned HTTP {e.response.status_code}: {detail}") from e
        except httpx.HTTPError as e:
            raise NotifyError(f"could not reach Telegram: {e}") from e


def send_test(token: str, chat_id: str) -> None:
    """Prove the connection at configure time.

    Credentials that are only exercised by the nightly job are credentials you discover are
    wrong on the morning you needed the report.
    """
    send(token, chat_id, "<b>Timar</b>\nTest message — notifications are working.")
