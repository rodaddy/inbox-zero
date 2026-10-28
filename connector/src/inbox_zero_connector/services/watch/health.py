"""The watcher's health file: one small JSON document, rewritten every round.

WHY A FILE AND NOT THE LEDGER (#64). The audit ledger is an append-only record
of what happened to MAIL, and reading "is the watcher alive?" out of it means
scanning to the end of a growing JSONL and inferring liveness from the absence
of rows -- which cannot distinguish a healthy watcher over a quiet mailbox from
a dead one. Liveness is a CURRENT-STATE question, so it gets a current-state
file: five fields, whole-file replace, no history. The ledger keeps its job.

WHAT READS IT. ``apps/cli/status.py`` renders it for a human, and
``services/notify.py`` is fired from the loop off the same transitions this
file records. Both read the same document, so what the operator is told by the
notification and what they see when they run ``status`` cannot disagree -- that
was the whole point of putting the state in one place rather than computing it
twice.

WRITTEN THROUGH ``utils.write_text_atomic``. A health file torn in half by a
crash is worse than none at all: ``status`` would report a parse failure at
exactly the moment the operator is trying to find out whether the watcher died.
Temp sibling, fsync, rename -- the same primitive the watch state uses.

CONTENT-FREE BY CONSTRUCTION. ``last_error`` carries an exception TYPE NAME,
never a message: the loop already discards tracebacks at its stream boundary
because exception text can quote a subject line, and this file would be a way
back around that.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from loguru import logger

from inbox_zero_connector.utils import write_text_atomic

HEALTH_FILENAME: Final = "health.json"
"""Name under the data directory. Beside the ledger, not inside it."""


@dataclass(frozen=True)
class Health:
    """The watcher's last-known state, as five independent facts.

    ``last_success_at`` is tracked SEPARATELY from ``last_round_at`` because the
    two answer different questions and a single timestamp answers neither. A
    watcher erroring every 60 seconds has a very recent ``last_round_at`` -- it
    is running, and it is running badly. The age that matters to an operator is
    the age of the last round that WORKED.

    ``token_expires_at`` is the REFRESH token's expiry, not the access token's.
    The access token expires hourly and renews itself invisibly, so its expiry
    would warn an operator about nothing; the refresh token expiring is the day
    the watcher stops. It is populated from the grant timestamp
    ``services.auth.grants`` records at auth time (#64).

    It is still ``None`` on three legitimate installs -- a Microsoft mailbox, a
    Google app the buyer has published, and an install that authenticated
    before the grant time was recorded -- because none of those has a
    predictable expiry to warn about. ``None`` therefore means "no advance
    warning is possible", never "expired".
    """

    last_round_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    token_expires_at: str | None = None


def _text(value: Any) -> str | None:
    """Return ``value`` when it is a string, else ``None``.

    The file on disk was written by SOME version of this code, not necessarily
    this one, so every field is validated on the way in. A wrong type degrades
    to the field's default rather than reaching ``status`` as a value that
    claims to be a timestamp and is not.
    """
    return value if isinstance(value, str) else None


def health_path(data_directory: Path) -> Path:
    """Return where the health file lives under ``data_directory``."""
    return data_directory / HEALTH_FILENAME


def write_health(data_directory: Path, health: Health) -> Path:
    """Write ``health`` atomically and return the path it landed at."""
    return write_text_atomic(
        health_path(data_directory),
        json.dumps(asdict(health), indent=2, sort_keys=True),
        what="HEALTH",
    )


def read_health(data_directory: Path) -> Health | None:
    """Return the recorded health, or ``None`` when there is none to read.

    ``None`` covers both "never written" and "unreadable", and the caller wants
    them merged: a status command's answer to either is the same sentence --
    this install has no health state, run the watcher. The unreadable case is
    logged so the difference is not lost, only kept out of the return type.
    """
    path = health_path(data_directory)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug(f"HEALTH: no health file yet path={path}")
        return None
    except OSError, ValueError:
        logger.opt(exception=True).error(f"HEALTH: unreadable health file path={path}")
        return None
    if not isinstance(payload, dict):
        logger.error(f"HEALTH: health file is not an object path={path}")
        return None
    failures = payload.get("consecutive_failures")
    return Health(
        last_round_at=_text(payload.get("last_round_at")),
        last_success_at=_text(payload.get("last_success_at")),
        last_error=_text(payload.get("last_error")),
        consecutive_failures=failures if isinstance(failures, int) else 0,
        token_expires_at=_text(payload.get("token_expires_at")),
    )


def record_round(
    data_directory: Path,
    *,
    error: str | None,
    now: datetime | None = None,
    token_expires_at: str | None = None,
) -> Health:
    """Fold one completed round into the health file and return what was written.

    The previous document is READ rather than recomputed, because
    ``consecutive_failures`` and ``last_success_at`` are the only two facts that
    span rounds and neither is available from the round itself. A missing or
    unreadable prior file starts from a fresh ``Health``, which loses a streak
    at most -- the alternative, refusing to write, would leave the operator with
    no health state precisely because the last one was damaged.

    Args:
        data_directory: Where the health file lives.
        error: The failing exception's type name, or ``None`` for a clean round.
        now: Injectable clock, for tests. Defaults to the current UTC time.
        token_expires_at: An explicit refresh-token expiry to record. When the
            caller passes ``None`` -- which every caller does today -- this
            module looks it up itself. The lookup lives HERE rather than in the
            loop because the health file is this module's document: a caller
            that forgot to pass the value would silently write a health file
            with a dead warning arm, which is exactly the #64 defect this
            change closes, and a default only fixes it for callers that
            remember to use it.

    Returns:
        The health document just written.
    """
    stamp = (now or datetime.now(UTC)).isoformat()
    previous = read_health(data_directory) or Health()
    expiry = token_expires_at or _looked_up_expiry(data_directory)
    health = Health(
        last_round_at=stamp,
        last_success_at=previous.last_success_at if error else stamp,
        last_error=error,
        consecutive_failures=previous.consecutive_failures + 1 if error else 0,
        token_expires_at=expiry,
    )
    write_health(data_directory, health)
    return health


def _looked_up_expiry(data_directory: Path) -> str | None:
    """Ask ``services.auth.grants`` when the refresh token expires. Never raises.

    Imported inside the function rather than at module scope because ``grants``
    reaches the config keystone and this module is imported by the watch loop's
    hot path; a lazy import keeps the health file's dependency on configuration
    at the moment it is actually needed. It also leaves the two modules' import
    order free, which matters because ``grants`` is the security module and
    nothing about liveness reporting should constrain how it loads.

    EVERY RECORDED PROVIDER IS ASKED, and this module never reads the configured
    one. Resolving ``settings().provider`` here would drag the whole settings
    singleton into the health write, and on a process that had not built it yet
    that call REBUILDS THE LOG SINKS mid-round -- observed as a torn-down sink
    in unrelated watch-loop tests. Asking the grant file instead keeps the
    health writer's dependencies to the data directory it was handed, and it is
    also the more correct answer on a two-mailbox install: the soonest expiry
    among the grants this install actually holds is the date the operator needs
    warning about, whichever provider happens to be configured today.

    A failure to determine the expiry is NOT a failure to record health. The
    round happened, and the liveness facts are the ones an operator is about to
    read; losing the advance warning is a degraded warning, while losing the
    health write is a watcher that looks dead. So the broad ``except`` is the
    correct shape here -- it is logged, and it costs one optional field.
    """
    from inbox_zero_connector.services.auth.grants import recorded_providers, refresh_token_expiry

    try:
        expiries = [
            expiry
            for provider in recorded_providers(data_directory)
            if (expiry := refresh_token_expiry(provider, data_directory)) is not None
        ]
    except Exception:  # ruff: ignore[blind-except] - advisory only; see docstring
        logger.opt(exception=True).error(
            "HEALTH: could not determine refresh-token expiry; re-auth warning disarmed"
        )
        return None
    return min(expiries).isoformat() if expiries else None
