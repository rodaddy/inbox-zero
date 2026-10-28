"""The ``watch`` verb's argument parser.

Lives outside ``main.py`` for the reason ``list_folders.py`` and
``show_scopes.py`` do: that module carries the parser and dispatch table for
every verb and sits against the repo's code-line ceiling
(``_DOCS/STANDARDS-python.md``). ``watch`` is the verb with the most flags, and
each one's help text is a CONTRACT rather than a label -- ``--once`` states the
four conditions a scheduler's exit status depends on, and ``--dry-run`` states
what a preview does not do -- so the block is long by necessity and does not
shrink by editing the prose down.

PARSER ONLY. The handler (``main._cmd_watch``), the exit contract
(``main._once_exit_code``), and the interval validator
(``main._watch_interval``) stay in ``main.py``: they are dispatched from there
and share its state, and moving them would be a refactor of code this split is
not about. This module registers arguments and returns.

NO NETWORK AND NO WATCH ENGINE AT IMPORT. Nothing here reaches
``services.watch``; ``main._cmd_watch`` still imports the loop inside its own
body, so the cold import stays cold (``tests/apps/cli/test_cold_import.py``).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable


def add_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    interval_type: Callable[[str], float],
    minimum_interval: float,
) -> None:
    """Register the ``watch`` verb and its three flags.

    Args:
        sub: The subparser action ``main.build_parser`` created.
        interval_type: ``main._watch_interval``, passed in rather than imported
            so this module does not import ``main`` back and create a cycle.
        minimum_interval: ``main.MIN_WATCH_INTERVAL_SECONDS``, for the help
            text, passed for the same reason.
    """
    watch_parser = sub.add_parser("watch", help="Watch the mailbox for new mail.")
    watch_parser.add_argument(
        "--interval",
        type=interval_type,
        default=60.0,
        help=f"Seconds per round. Minimum {minimum_interval:g}.",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run one polling pass and exit. Exit 0 ONLY when a pass ran, "
            "polled at least one mail stream, filed every message it decided "
            "on, and recorded no error. Exit 1 when any filing failed, when "
            "the pass errored (expired or missing credential, provider "
            "outage), when the configured scope selected no stream, or when "
            "no pass ran at all. The continuous watch always exits 0."
        ),
    )
    watch_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "PREVIEW ONLY. Poll and classify exactly as a real pass would, then "
            "print what WOULD be filed and which destination folders do not "
            "exist yet, without moving any mail, writing any receipt, or "
            "storing the stream position. Requires --once. Exit 0 when the "
            "preview ran."
        ),
    )
