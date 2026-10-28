"""Tell the operator whether a newer build of THIS kit exists (#67, D13).

NOTHING HERE REPLACES ANYTHING. Rico's decision D13 (2026-09-01) is a version
check plus a GUIDED update: the installed copy learns that a newer build was
promoted and prints the one path to it, and the operator downloads and runs the
installer themselves. A connector that rewrites its own files is a connector
that can brick a mailbox integration at 3am with nobody watching, and the whole
value of this kit is that its blast radius is legible.

WHY A NETWORK FAILURE IS NEVER FATAL. This verb is a convenience wrapped around
someone else's uptime -- DNS, a proxy, a laptop on a plane. If it exited
non-zero on a timeout, a scheduler that ran it would report a healthy install as
broken, and an operator who learned to ignore that would ignore the real signal
too. Unreachable is reported as unreachable (exit 3, harness error) and nothing
about the install is implied.

WHAT IT READS. The version comes from the root ``VERSION`` file the packaging
step ships, and the tier from the root ``TIER`` file the same step writes -- the
tier is a BUILD OUTPUT of one shared tree (#107), so an installed copy has no
other way to know which cut it is. A copy with no ``TIER`` (a working checkout,
a hand-assembled custom build) is told so plainly rather than guessed at.

Its stdout IS its product, like every other module in this CLI directory, and it
never runs in the server process, so nothing it prints can reach the MCP
protocol channel.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
from loguru import logger
from packaging.version import InvalidVersion, Version

from inbox_zero_connector.config_paths import kit_links_base_url

KIT_SLUG = "inbox-zero"
"""This kit's slug in the release manifest."""

KIT_PAGE_URL = "https://rtech-consulting.com/kits/inbox-zero"
"""Where a newer zip is actually obtained. Named in the message because
"an update exists" without the path to it is a dead end for the reader."""

# The install root: this file is src/inbox_zero_connector/apps/cli/update.py, so
# the zip root -- where VERSION and TIER sit -- is four parents up from src/.
_ROOT = Path(__file__).resolve().parents[4]

TIMEOUT_SECONDS = 5.0
"""Short on purpose: this verb is worth waiting five seconds for and no more."""


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``update-check`` verb on the CLI's subparser action.

    The row lives here rather than inline in ``build_parser`` for the same
    reason ``undo.add_parser`` does: ``main.py`` sits against the repo's file
    and function code-line ceilings, and a verb that owns its own module can own
    its own row.

    Args:
        sub: The subparser action every verb is registered on.
    """
    sub.add_parser(
        "update-check",
        help="Ask whether a newer build of this kit has been published.",
        description=(
            "Compare this install's version against the published build for its "
            "tier and print what to do. Reads a public, read-only endpoint; "
            "downloads nothing and changes nothing. A network failure is "
            "reported, never fatal."
        ),
    )


def _read_root_file(name: str) -> str:
    """Return the stripped contents of a root marker file, or "" if unreadable."""
    try:
        return (_ROOT / name).read_text(encoding="utf-8").strip()
    except OSError:
        # Expected on a source checkout and on a partial unzip: the marker is
        # absent, the verdict degrades to "unknown", and nothing is fatal. Debug
        # rather than warning because a missing marker is the common case.
        logger.debug(f"UPDATE: root marker unreadable name={name!r}")
        return ""


def _fetch_stable(tier: str) -> str | None:
    """Return the promoted version for ``tier``, or None when none is promoted.

    Raises:
        RuntimeError: The endpoint could not be reached or did not answer with
            a usable payload. The caller turns this into "could not check".
    """
    url = f"{kit_links_base_url()}/v/{KIT_SLUG}/{tier}"
    try:
        response = httpx.get(url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise RuntimeError(f"{type(error).__name__} reaching {url}") from error
    stable = payload.get("stable")
    if stable is not None and not isinstance(stable, str):
        raise RuntimeError(f"{url} answered a non-string version")
    return stable


def cmd_update_check() -> int:
    """Print exactly one verdict line and return its exit code.

    Returns:
        ``0`` when this install is current, ``1`` when a newer build exists --
        an actionable state a scheduler should be able to see -- and ``3`` when
        the question could not be answered at all.
    """
    installed = _read_root_file("VERSION")
    tier = _read_root_file("TIER")
    if not installed or not tier:
        missing = "VERSION" if not installed else "TIER"
        print(f"could not check (this copy has no {missing} file, so it is not a built kit)")
        return 3
    try:
        stable = _fetch_stable(tier)
        if stable is None:
            print(f"up to date ({installed})")
            return 0
        newer = Version(stable) > Version(installed)
    except (RuntimeError, InvalidVersion) as error:
        logger.trace("update check failed: {}", type(error).__name__)
        print(f"could not check ({error})")
        return 3
    if not newer:
        print(f"up to date ({installed})")
        return 0
    print(
        f"update available: {installed} -> {stable}. Get the new zip from "
        f"{KIT_PAGE_URL} and run install.sh/install.ps1 from it; your config, "
        "keychain entries and ledger are kept."
    )
    return 1
