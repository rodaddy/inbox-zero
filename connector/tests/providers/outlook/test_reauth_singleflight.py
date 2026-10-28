"""Concurrent 401s must produce exactly ONE re-authentication (#44).

WHY THIS EXISTS
    ``with_reauth_once`` had no mutual exclusion, so N workers whose access
    token expired together each ran the whole refresh: N unattended MSAL round
    trips against Entra, and N writes to the same keychain entry where the last
    writer wins. The symptom is not an exception -- every caller "succeeds" --
    which is exactly why the assertion has to COUNT refreshes rather than watch
    for an error.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from inbox_zero_connector.providers.outlook.categories import GraphStatusError
from inbox_zero_connector.providers.outlook.reauth import with_reauth_once

WORKERS = 8


class CountingProvider:
    """A ``Refreshable`` whose sign-in is counted and deliberately slow.

    The sleep is what makes the race reproducible rather than timing-dependent:
    it holds the first refresher inside ``authenticate`` long enough for every
    other thread to arrive at the same branch, which is the window the missing
    lock left open.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._client = None
        self.refreshes = 0
        self._count_lock = threading.Lock()

    def authenticate(self, interactive: bool = False) -> str:
        with self._count_lock:
            self.refreshes += 1
        time.sleep(0.05)
        return "ok"


def test_concurrent_unauthorized_refreshes_once() -> None:
    provider = CountingProvider()
    seen: list[str] = []
    seen_lock = threading.Lock()
    attempted: set[int] = set()
    attempt_lock = threading.Lock()

    def make_send(worker: int) -> Callable[[], str]:
        def send() -> str:
            with attempt_lock:
                first = worker not in attempted
                attempted.add(worker)
            if first:
                raise GraphStatusError("unauthorized", 401)
            return f"ok-{worker}"

        return send

    def run(worker: int) -> None:
        result = with_reauth_once(provider, "test", make_send(worker))
        with seen_lock:
            seen.append(result)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert provider.refreshes == 1
    assert sorted(seen) == sorted(f"ok-{index}" for index in range(WORKERS))


def test_non_unauthorized_error_never_refreshes() -> None:
    provider = CountingProvider()

    def send() -> str:
        raise GraphStatusError("server blew up", 500)

    with pytest.raises(GraphStatusError):
        with_reauth_once(provider, "test", send)
    assert provider.refreshes == 0
