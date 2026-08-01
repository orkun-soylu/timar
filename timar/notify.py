"""Telegram delivery.

A push channel matters more here than in most tools: the machines Timar watches are the ones
nobody is looking at. A report that only exists in a web page is a report nobody reads until
they already suspect something, which is exactly the wrong time.

Notification is optional — everything works without it, the findings just stay in the UI.
"""
from __future__ import annotations

import html
import logging

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


def _split(text: str) -> list[str]:
    """Break an over-long message on line boundaries, never mid-line.

    Splitting on a raw character count can cut an HTML tag in half, and Telegram then rejects
    the fragment.
    """
    if len(text) <= MAX_MESSAGE:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_MESSAGE:
            if current:
                chunks.append(current)
            # A single line longer than the limit still has to go somewhere.
            current = line[:MAX_MESSAGE]
        else:
            current = candidate
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
