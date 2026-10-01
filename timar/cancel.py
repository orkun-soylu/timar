"""Stopping a running job — cooperatively, at the boundary between two steps.

A job runs in a worker thread, and Python cannot stop a thread from outside without leaving
whatever it was doing half done: an `apt-get` cut mid-transaction, a machine woken for the run and
never shut down again. So a stop is a request. The job looks at it before each host and each
compose project, and when it is set, it finishes the step in progress — including putting a woken
machine back to sleep — and goes no further.
"""
from __future__ import annotations

import threading
from collections import defaultdict

_requests: dict[str, threading.Event] = defaultdict(threading.Event)


def request(name: str) -> None:
    _requests[name].set()


def requested(name: str) -> bool:
    return _requests[name].is_set()


def clear(name: str) -> None:
    _requests[name].clear()
