"""The ``status`` verb: is the watcher alive, and does it need me right now (#64).

THE ONE QUESTION THIS ANSWERS is the one no other command in this CLI does.
``diagnose`` builds a bundle to MAIL SOMEBODY -- it is the second step, taken
once a human already knows something is wrong. ``digest`` reports on mail, and
a watcher that has stopped produces no digests, so its silence is the failure
rather than a report of it. This verb reads the health file the loop writes at
the end of every round and turns it into three words and an exit code.

WHY STALENESS IS TWO INTERVALS. One missed round is ordinary -- a laptop lid, a
WAN blip, a pass that ran long -- and a status command that cries stale on it
gets ignored, which costs the same as no command. Two intervals cannot be a
single late round. The interval is the operator's own ``--interval``, so the
threshold tracks whatever they configured rather than a number baked in here.

STALE IS NOT FAILING, and keeping them apart is the point of having three
states. A FAILING watcher is running and erroring -- the rounds are happening,
the mail is not being triaged, and the ``last_error`` names the type. A STALE
watcher is not running at all: no process, no rounds, nothing to read an error
off. They send an operator to different places (the log versus the service
manager), so they are never merged into one "unhealthy".

Its stdout IS its product, like every other module in this directory, and it
never runs in the server process, so nothing it prints can reach the MCP
protocol channel. It prints times, counts, and an exception type name -- the
health file is content-free by construction and this reader adds nothing.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import Final

from loguru import logger

from inbox_zero_connector.config import data_dir
from inbox_zero_connector.services.watch.health import Health, read_health

EXIT_HEALTHY: Final = 0
EXIT_NEEDS_ATTENTION: Final = 1
EXIT_NO_HEALTH_FILE: Final = 3
"""3 is the harness-error code this repo's checks use, and it is right here too:
no health file means this command could not answer the question, which is a
different thing from answering "unhealthy"."""

NEEDS_ATTENTION_STATE: Final = "needs attention"
"""The state word for a watcher whose rounds are clean but whose credential is
about to die. ``healthy`` beside exit 1 is the defect this names away: the word
and the code are two halves of one answer and a monitor that disagrees with
itself is read as broken. ``failing`` and ``stale`` are both already taken by
conditions of the LOOP, and this is a condition of the CREDENTIAL, so it needs
its own word rather than a borrowed one."""

STALE_INTERVAL_MULTIPLE: Final = 2.0
"""Missed rounds before a watcher is called stale. See the module docstring."""

REAUTH_WARNING_WINDOW: Final = timedelta(hours=24)
"""How close to expiry the credential gets before the operator is told."""

JUST_NOW_SECONDS: Final = 1.0
"""Below this an age reads "just now". A rendered "0s ago" is indistinguishable
from a clock that never advanced, and the operator cannot tell the difference
between a fresh round and a broken timestamp."""

MINUTE_BOUNDARY_SECONDS: Final = 90.0
"""Below this an age reads in seconds. 90 rather than 60 so "75s ago" is not
rounded to a bare "1m ago" the operator cannot tell from 89 seconds."""

HOUR_BOUNDARY_SECONDS: Final = 5400.0
"""Below this an age reads in minutes. Same reasoning, one unit up."""

DEFAULT_INTERVAL_SECONDS: Final = 60.0
"""Matches ``services/watch/loop.DEFAULT_INTERVAL_SECONDS``. Restated rather
than imported so the CLI's status reader does not pull the loop -- and its
provider and classifier imports -- into a command that only reads a file."""


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``status`` verb on the CLI's subparser action.

    The row lives here rather than inline in ``build_parser`` for the same
    reason ``undo.add_parser`` does: ``main.py`` sits against the repo's file
    and function code-line ceilings, and a verb that owns its own module can
    own its own row.

    Args:
        sub: The subparser action every verb is registered on.
    """
    parser = sub.add_parser(
        "status",
        help="Is the watcher healthy? Prints its state and exits 0/1/3.",
        description=(
            "Read the health file the watcher writes at the end of every round and "
            "report whether it is healthy, stale, or failing. Exits 0 when healthy, "
            "1 when it needs attention, and 3 when no health file exists yet."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=(
            "The watcher's configured interval in seconds. Staleness is judged at "
            f"{STALE_INTERVAL_MULTIPLE:g}x this. Default: %(default)s."
        ),
    )


def _parse(stamp: str | None) -> datetime | None:
    """Read one ISO timestamp off the health file, or ``None`` if unusable."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        # Fail open: a corrupt stamp reads as "unknown", never as a crash. It
        # is logged rather than printed because the readout's own three lines
        # are the product and a parser complaint does not belong among them.
        logger.warning(f"STATUS: unparseable health timestamp value={stamp!r}")
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age(moment: datetime | None, now: datetime) -> str:
    """Render how long ago ``moment`` was, in the largest sensible unit."""
    if moment is None:
        return "never"
    seconds = max(0.0, (now - moment).total_seconds())
    if seconds < JUST_NOW_SECONDS:
        return "just now"
    if seconds < MINUTE_BOUNDARY_SECONDS:
        return f"{seconds:.0f}s ago"
    if seconds < HOUR_BOUNDARY_SECONDS:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _state(health: Health, interval_seconds: float, now: datetime) -> str:
    """Return ``healthy``, ``stale``, or ``failing`` for this health document.

    FAILING IS CHECKED FIRST because it is the more actionable of the two
    unhealthy readings: a watcher erroring every round is also, eventually, one
    whose last SUCCESS is old, and reporting that as staleness would send the
    operator to the service manager to restart a process that is already
    running.
    """
    if health.consecutive_failures > 0 or health.last_error:
        return "failing"
    last_success = _parse(health.last_success_at)
    if last_success is None:
        return "stale"
    limit = timedelta(seconds=interval_seconds * STALE_INTERVAL_MULTIPLE)
    return "stale" if now - last_success > limit else "healthy"


def _reauth_line(health: Health, now: datetime) -> str | None:
    """Return the re-auth warning when the credential is near expiry or past it.

    THE PAST TENSE IS A SEPARATE SENTENCE, not the same one with an old date in
    it. A refresh window that closed yesterday rendered as "needed by
    <yesterday>" reads as a deadline the operator still has time to meet, when
    in fact the watcher has already stopped being able to renew -- the two
    situations call for different urgency and so they get different words.
    """
    expires = _parse(health.token_expires_at)
    if expires is None or expires - now > REAUTH_WARNING_WINDOW:
        return None
    stamp = expires.date().isoformat()
    if expires <= now:
        return f"re-auth OVERDUE since {stamp}: the watcher cannot refresh its token"
    return f"re-auth needed by {stamp}"


def cmd_status(interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> int:
    """Print the watcher's state and return the process exit code.

    Args:
        interval_seconds: The watcher's configured interval, which sets the
            staleness threshold at ``STALE_INTERVAL_MULTIPLE`` times it.

    Returns:
        0 healthy, 1 needs attention, 3 no health file to read.
    """
    health = read_health(data_dir())
    if health is None:
        print("No health file yet. Run `inbox-zero-connector watch` at least once.")
        return EXIT_NO_HEALTH_FILE
    now = datetime.now(UTC)
    reauth = _reauth_line(health, now)
    state = _state(health, interval_seconds, now)
    if state == "healthy" and reauth is not None:
        state = NEEDS_ATTENTION_STATE
    print(f"Watcher: {state}")
    print(f"Last successful run: {_age(_parse(health.last_success_at), now)}")
    if health.consecutive_failures:
        print(f"Consecutive failures: {health.consecutive_failures} ({health.last_error})")
    if reauth is not None:
        print(reauth)
    return EXIT_HEALTHY if state == "healthy" else EXIT_NEEDS_ATTENTION
