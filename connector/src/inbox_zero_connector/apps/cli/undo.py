"""Render the ``undo`` preview, and on ``--apply`` perform the reversal (#66).

DRY RUN IS THE DEFAULT AND IT IS STILL THE DEFAULT AFTER ``--apply`` EXISTS.
Running ``undo`` with no flags prints what would happen and touches nothing, so
the destructive reading of this command is the one an operator has to ask for by
name. That is the guarantee worth keeping; what changed is that asking is now
possible.

IT USED TO BE THE ONLY MODE, and the reason was a gap in the audit spine rather
than caution: the ledger recorded a filing's DESTINATION and no source, so the
plan had to be read from the decision trace -- a file its own module documents
as a diagnostic -- and a mailbox write authorised by a diagnostic was the trade
the command declined to make. Schema 4 closed that at the receipt boundary
(``services/audit/ledger.py``: ``run_id`` and ``source_container``), so the plan
now comes from RECEIPTS and ``--apply`` is authorised by the same evidence that
proves the filing happened. ``services/audit/undo.py`` holds the full argument.

WHAT AN OLD ROW GETS IS THE OLD ANSWER: refused, never guessed, and COUNTED so
the operator is told the reversal was partial rather than left to assume it was
whole.

Its stdout IS its product, like every other module in this package's CLI
directory, and it never runs in the server process, so nothing it prints can
reach the MCP protocol channel. Identifiers and the operator's own folder names
only -- ``Reversal`` carries no message content by construction.
"""

from __future__ import annotations

import argparse

from inbox_zero_connector.config import load_settings
from inbox_zero_connector.providers import get_provider
from inbox_zero_connector.services.audit import undo as undo_service


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``undo`` verb on the CLI's subparser action.

    The row lives here rather than inline in ``build_parser`` for the same
    reason ``service_unit.add_parser`` does: ``main.py`` sits against the repo's
    file and function code-line ceilings, and a verb that owns its own module
    can own its own row.

    Args:
        sub: The subparser action every verb is registered on.
    """
    parser = sub.add_parser(
        "undo",
        help="Put a watch run's filings back. Previews by default; --apply moves mail.",
        description=(
            "Print the moves that would put recently filed mail back where it came "
            "from, and list the folders the run created. Reads the ledger's own "
            "receipts, so it never guesses a source folder. Previews only unless "
            "--apply is given; --apply moves mail and receipts every reversal."
        ),
    )
    # --last and --run are the same question -- WHICH run -- so they are mutually
    # exclusive rather than two flags whose combination needs a precedence rule
    # nobody would remember. Neither given is --last, because an operator typing
    # `undo` means the thing that just happened.
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--last",
        action="store_true",
        help="Reverse the most recent run that filed anything. The default.",
    )
    which.add_argument(
        "--run",
        metavar="ID",
        default="",
        help="Reverse one specific run, by the run id shown in the ledger.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move the mail back. Without this, nothing is changed.",
    )


def cmd_undo(run: str = "", apply_changes: bool = False) -> int:
    """Print the reversal table, and move the mail back when asked to.

    An EMPTY plan is printed as such rather than silently producing nothing: a
    run that filed nothing and a command that failed before printing look
    identical on a blank terminal, and only one of them means the mailbox was
    left alone on purpose.

    Args:
        run: A specific run id, or "" for the most recent run that filed.
        apply_changes: Move the mail. False prints the plan and changes nothing.

    Returns:
        ``0`` when the preview was produced, or when every step of an applied
        reversal moved. ``1`` when ``--apply`` was asked for and at least one
        message did not move -- the mailbox is then partly restored, which is a
        state a scheduler must be able to see in an exit code.
    """
    run_id = run or undo_service.last_run_id()
    plan = undo_service.plan(run_id)
    if not plan.steps:
        _print_empty(run_id, plan)
        return 0
    verb = "were moved back" if apply_changes else "would be moved back"
    print(f"Undo ({run_id}): {len(plan.steps)} message(s) {verb}.")
    for step in plan.steps:
        print(f"  {step.message_id}  {step.folder_from} -> {step.folder_to}")
    _print_skipped(plan)
    if plan.created:
        print(f"\nCREATED by the run: {len(plan.created)} folder(s). Undo never deletes a folder:")
        for folder in plan.created:
            print(f"  {folder}")
    if not apply_changes:
        print("\nNothing was changed. Re-run with --apply to move this mail back.")
        return 0
    return _apply(plan)


def _apply(plan: undo_service.UndoPlan) -> int:
    """Perform the reversal and report what actually moved."""
    settings = load_settings()
    moved = undo_service.apply(get_provider(settings.provider), plan)
    print(f"\nMoved back: {moved} of {len(plan.steps)}. Every reversal wrote its own receipt.")
    if moved == len(plan.steps):
        return 0
    # A PARTIAL RESTORE IS A NON-ZERO EXIT, because the mailbox is now in a state
    # neither the operator nor a scheduler asked for: some mail is back and some
    # is not. The FAILED receipts name which.
    print(
        f"{len(plan.steps) - moved} message(s) did not move. Each one wrote a "
        "FAILED undo receipt naming the provider's error code; the mailbox is "
        "partly restored."
    )
    return 1


def _print_empty(run_id: str, plan: undo_service.UndoPlan) -> None:
    """Say WHY there is nothing to do -- the three reasons are not the same."""
    if not run_id:
        print("Undo: no watch run has filed anything yet. Nothing to put back.")
        return
    if plan.skipped_unreversible:
        print(
            f"Undo ({run_id}): {plan.skipped_unreversible} filing(s) found, none "
            "reversible. Their receipts predate the schema that records a "
            "filing's source folder, so undo will not guess where they came "
            "from. Move them in your mail client."
        )
        return
    print(f"Undo ({run_id}): no applied filings in this run. Nothing to put back.")


def _print_skipped(plan: undo_service.UndoPlan) -> None:
    """Announce the partial plan. Silence here would read as a complete one."""
    if not plan.skipped_unreversible:
        return
    print(
        f"\nSKIPPED: {plan.skipped_unreversible} filing(s) in this run have no "
        "recorded source folder (their receipts predate schema 4). Undo does not "
        "guess -- move those in your mail client."
    )
