"""The ``digest`` subcommand: one day's audit digest, on stdout or as a draft.

Lives outside ``main.py`` for the same reason ``list_folders.py``,
``show_scopes.py``, ``desktop.py``, and ``voice.py`` do: that module carries the
parser and dispatch table for every verb and sits against the repo's code-line
ceiling. This verb is a self-contained render over the ledger and shares no
state with any other command.

Its stdout IS its product. A digest cannot be delivered through the logger,
which writes to sinks, and it never runs in the server process, so nothing it
prints can reach the MCP protocol channel.

``--as-draft`` is the DELIVERY arm (#65, decision D10/D20 2026-09-01): the
same rendered text, put into the account's own drafts folder through
``provider.create_self_draft``, which has no recipient parameter and cannot
send. A terminal the owner is not looking at is not delivery, and every other
channel that delivers mail sends mail; a draft in the owner's own mailbox is
the one that does neither.

HOW ``--as-draft`` REACHES ``cmd_digest`` -- read this before changing it. The
dispatch row in ``main.py`` is ``digest.cmd_digest(args.day)`` and passes the
day and nothing else, so a second namespace attribute has no route through it.
``_AsDraft`` therefore records the flag on this module when argparse sees it,
and ``cmd_digest`` reads and CLEARS it. The clear is what keeps the state from
outliving one invocation, which matters because the test suite parses many
command lines in one process. The alternative -- widening the dispatch row --
is a one-line change in a file this lane is fenced out of; when that fence is
gone, a ``cmd_digest(args.day, args.as_draft)`` row and a plain
``store_true`` replaces all of this, and that is the preferred shape.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from loguru import logger

from inbox_zero_connector.config import load_config, load_settings
from inbox_zero_connector.providers import ProviderError, get_provider
from inbox_zero_connector.services.audit import ledger

_as_draft_requested = False
"""Set by ``_AsDraft``, read and cleared by ``cmd_digest``. See module docstring."""


class _AsDraft(argparse.Action):
    """Record ``--as-draft`` where ``cmd_digest`` can find it without a namespace.

    A zero-argument action rather than ``store_true`` because the value has to
    land on the MODULE, not on the namespace: the namespace attribute would
    never be read. It still sets the namespace attribute too, so ``--help`` and
    any future dispatch row that does pass it keep working.
    """

    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        """Fix the action to take no operand and start out false.

        ``--as-draft`` is a bare flag, so ``nargs=0`` is what makes argparse
        invoke this action instead of demanding a value. The default is pinned
        here rather than at each call site so every ``add_argument`` row using
        this action gets the same falsy starting point.
        """
        super().__init__(option_strings, dest, nargs=0, default=False, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        """Raise the module flag ``cmd_digest`` reads, and mirror it on the namespace.

        The module global is the one that matters: ``cmd_digest`` is dispatched
        without the namespace, so a ``store_true`` value would be written and
        never read. The namespace attribute is set anyway so ``--help`` output
        and any future namespace-passing dispatch row stay correct.
        """
        global _as_draft_requested
        _as_draft_requested = True
        setattr(namespace, self.dest, True)


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``digest``, its optional ISO date operand, and ``--as-draft``."""
    parser = sub.add_parser("digest", help="Print the audit digest for a day.")
    parser.add_argument("day", nargs="?", help="ISO date, defaults to today (UTC).")
    parser.add_argument(
        "--as-draft",
        action=_AsDraft,
        help="Also leave the digest as a draft in your own mailbox. Never sends.",
    )


def _deliver_as_draft(rendered: str, target: date | None) -> int:
    """Put the rendered digest into the owner's own drafts folder.

    POSTURE WINS. When the account's drafts posture is off there is no draft to
    create, and this refuses in one sentence naming the flag rather than
    falling back to stdout: a command asked to deliver, that prints instead, has
    told the operator it succeeded at the thing they did not ask for.

    Args:
        rendered: The digest text, byte-identical to what stdout would carry.
        target: The digest's day, or ``None`` for today, for the subject line.

    Returns:
        ``0`` when the draft was created, ``1`` on a posture refusal or a
        provider failure.
    """
    settings = load_settings()
    # ``load_config().drafts_allowed`` is the ONE sanctioned way to ask whether a
    # draft may be composed -- the AND of posture and the provider's scope flag.
    # ``services/voice/drafting.py`` asks it the same way; comparing posture by
    # hand here would be a second implementation of the narrowing rule.
    if not load_config().drafts_allowed(settings.provider):
        print(
            f"Drafts are off for this account, so --as-draft cannot deliver the digest: "
            f"set drafts_enabled.{settings.provider} in the connector config "
            f"(and posture to file_read_draft) and re-run auth.",
            file=sys.stderr,
        )
        return 1
    day = target or datetime.now(UTC).date()
    subject = f"Inbox Zero digest {day.isoformat()}"
    provider = get_provider(settings.provider, drafts_enabled=True)
    try:
        draft_id = provider.create_self_draft(subject, rendered)
    except ProviderError as exc:
        logger.opt(exception=True).error("DIGEST: create_self_draft failed")
        print(f"Could not leave the digest as a draft: {exc}", file=sys.stderr)
        return 1
    print(f"Left the digest in your drafts ({subject}), addressed to nobody. Id: {draft_id}")
    return 0


def cmd_digest(day_arg: str | None) -> int:
    """Print one day's audit digest, rendered by the ledger itself.

    The date is parsed here so a malformed argument fails at the CLI boundary
    naming the input, instead of reaching the ledger and reading as an empty day
    -- "no activity" and "you typed the date wrong" must not look the same in an
    audit surface. The ValueError is CAUGHT rather than allowed to escape: a
    traceback is a crash report, and a typo is not a crash. It reads as the
    audit surface being broken at exactly the moment somebody is auditing.

    Args:
        day_arg: An ISO ``YYYY-MM-DD`` date, or ``None`` for the ledger's own
            default day.

    Returns:
        ``0`` with the digest on stdout, or ``1`` with an operator error on
        stderr when ``day_arg`` is not an ISO date or when ``--as-draft`` was
        asked for on an account whose drafts posture is off.
    """
    global _as_draft_requested
    as_draft, _as_draft_requested = _as_draft_requested, False
    try:
        target = date.fromisoformat(day_arg) if day_arg else None
    except ValueError:
        logger.trace("digest: rejected non-ISO date argument")
        print(
            f"Not a date: {day_arg!r}. Expected ISO YYYY-MM-DD, for example "
            f"{datetime.now(UTC).date().isoformat()}. Omit the argument for today.",
            file=sys.stderr,
        )
        return 1
    rendered = ledger.daily_digest(target)
    print(rendered)
    if as_draft:
        return _deliver_as_draft(rendered, target)
    return 0
