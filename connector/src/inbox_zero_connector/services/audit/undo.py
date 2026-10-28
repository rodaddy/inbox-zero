"""Reverse a watch run's filings -- planned from the LEDGER, and now applied (#66).

WHY THIS MODULE CHANGED SHAPE, STATED FIRST BECAUSE IT IS THE WHOLE POINT. It
used to ship as a preview and nothing else, and the reason was a gap in the
audit spine rather than caution: reversing a filing needs the folder the message
came FROM, and the ledger recorded only the destination. So the plan was read
from the DECISION TRACE, which does record it and which its own module documents
as a diagnostic -- and executing mailbox writes off a diagnostic would promote a
file that says "never a substitute for a ledger receipt" into the evidence that
authorises a mutation. That trade was declined, correctly.

THE GAP IS CLOSED AT THE RECEIPT BOUNDARY, WHICH IS WHERE IT HAD TO BE. Schema 4
adds ``run_id`` and ``source_container`` to every row
(``services/audit/ledger.py``), stamped by ``services/watch/round.py`` as the
filing happens. So the reversal is now planned from RECEIPTS: the same file that
proves the filing happened is the file that says where to put it back, and no
diagnostic authorises a write.

WHAT AN OLD ROW GETS: REFUSED, NEVER GUESSED. A v1-v3 row carries an empty
``source_container``, and this module skips it rather than inventing an origin.
That is the same refusal the preview-only version was built around, now scoped
to the rows that actually lack the field instead of to every row.

A REFUSED FILING IS NOT REVERSED. ``Status.FAILED`` means the provider declined
and the message never moved, so a reversal would move mail that was already
where it belonged -- the plan's own bug, wearing the shape of a fix.

AN UNDO IS NOT REVERSED EITHER, and that is what stops a double ``undo --last``
from putting mail back where the operator just took it from. A reversal writes
its own ``ACTION_UNDO`` receipt, and only ``ACTION_FILE`` rows are candidates.

EVERY REVERSAL WRITES ITS OWN RECEIPT. It is a provider write -- it moves mail --
so it owes a row exactly as the filing it reverses owed one, appended beside it.
Nothing here edits or deletes the original row: a wrong row is corrected by a
later row (``services/audit/README.md``), which is why an undone filing still
reads as having happened, because it did.

CREATED FOLDERS ARE LISTED, NEVER DELETED. A folder the run created may since
have been used by the owner for something else, and this module has no way to
know. The list is a prompt for a human, not a work queue.

Identifiers only, like every other module in this package's audit path: a
message id and folder NAMES the operator's own taxonomy chose. No subject, no
sender, no body.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel, ConfigDict

from inbox_zero_connector.providers.base import MailProvider
from inbox_zero_connector.services.audit import ledger


class Reversal(BaseModel):
    """One filing that happened, and where putting it back would send it.

    Frozen and content-free by construction, matching ``trace.TraceRow`` and
    ``watch/preview.PlannedFiling``: this is printed to a terminal and pasted
    into support threads, so there is no field on it that can hold mail text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    """The provider's id for the message. Not its subject and not its sender."""

    provider: str
    """``"google"`` or ``"microsoft"`` -- whose mailbox this row belongs to.

    Carried so ``apply`` can refuse a row from a provider other than the one it
    was handed. A ledger holds every account's rows, and filing a Graph id
    through the Gmail adapter is a write against the wrong mailbox.
    """

    folder_from: str
    """Where the run put it -- the receipt's ``detail``, the filing's destination."""

    folder_to: str
    """Where it was before the run -- the receipt's ``source_container``.

    This is the value the ledger gained in schema 4, and it is why the plan is
    now read from receipts rather than from the trace. See the module docstring.
    """

    run_id: str
    """The run that filed it, so a reversal's own receipt can name what it undid."""


class UndoPlan(BaseModel):
    """What reversing a run would do, and what it would deliberately leave alone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[Reversal, ...]
    """One entry per filing that actually reached the mailbox, in ledger order."""

    created: tuple[str, ...]
    """Destination folders the mailbox does not have, sorted and de-duplicated.

    LISTED FOR A HUMAN. Nothing in this module deletes a folder or a label.
    """

    run_id: str = ""
    """The run these steps belong to, or "" when the plan is empty."""

    skipped_unreversible: int = 0
    """Applied filings in the selected run that carry no ``source_container``.

    COUNTED RATHER THAN DROPPED SILENTLY, because the operator needs to know the
    reversal is partial. These are pre-schema-4 rows: the receipt boundary never
    recorded where their messages came from, so putting them back would be a
    guess, and this module refuses to guess. They are reported so the operator
    can move them by hand rather than believing the mailbox was fully restored.
    """


def last_run_id(path: Path | None = None) -> str:
    """The most recent run that filed anything, or "" if none has.

    THE ROW ORDER IS THE ANSWER, not the timestamp. The ledger is append-only,
    so the last ``ACTION_FILE`` row in the file is by construction from the most
    recent run that filed -- and comparing timestamps would be strictly worse,
    because they are whole seconds and two runs can share one.

    Args:
        path: Ledger file to read. Defaults to the installed ledger.

    Returns:
        The run id, or "" when no filing receipt carries one.
    """
    runs = [
        entry.run_id
        for entry in ledger.read_all(path)
        if entry.action == ledger.ACTION_FILE and entry.run_id
    ]
    return runs[-1] if runs else ""


def plan(
    run_id: str,
    path: Path | None = None,
    existing: frozenset[str] | None = None,
) -> UndoPlan:
    """Read the ledger and say what reversing ``run_id``'s filings would do.

    Reads one file and writes nothing -- no mailbox call, no ledger row, no edit
    to the receipts it read.

    Args:
        run_id: The run to reverse. An empty string selects nothing, which is
            the correct answer for a ledger that has no runs in it rather than
            an error -- see ``last_run_id``.
        path: Ledger file to read. Defaults to the installed ledger.
        existing: Container names the mailbox currently has, for the created
            list. ``None`` means the listing was not gathered, and every
            destination is then reported as created rather than silently
            dropped -- an over-report a human can dismiss beats an under-report
            that hides a folder.

    Returns:
        The reversal plan. Empty steps mean the run filed nothing that landed,
        or that everything it landed predates schema 4.
    """
    rows = [
        entry
        for entry in ledger.read_all(path)
        if entry.action == ledger.ACTION_FILE
        and entry.status is ledger.Status.APPLIED
        and entry.run_id == run_id
        and run_id
    ]
    steps = tuple(
        Reversal(
            message_id=entry.message_id,
            provider=entry.provider,
            folder_from=entry.detail,
            folder_to=entry.source_container,
            run_id=entry.run_id,
        )
        for entry in rows
        if entry.source_container
    )
    have = existing if existing is not None else frozenset()
    created = tuple(sorted({entry.detail for entry in rows if entry.detail} - have))
    return UndoPlan(
        steps=steps,
        created=created,
        run_id=run_id,
        skipped_unreversible=len(rows) - len(steps),
    )


def apply(provider: MailProvider, undo_plan: UndoPlan) -> int:
    """Move every step's message back, receipting each reversal. WRITES MAIL.

    ONE PROVIDER CALL AND ONE LEDGER ROW PER STEP, in that order, which is the
    same ordering every other write in this package uses: the mutation happens,
    then it is receipted, and a receipt is never written for a call that did not
    return. A step whose provider call raises is receipted FAILED and the loop
    CONTINUES -- one contended message must not strand the rest of a reversal
    half-done, and the FAILED row is what tells the operator which ones to
    handle by hand.

    A ROW FROM ANOTHER PROVIDER IS REFUSED, not attempted. The ledger holds
    every configured account's receipts, so a plan built from it can name a
    mailbox this adapter does not serve; filing a Graph id through the Gmail
    adapter is a write against the wrong mailbox, and it would be receipted as
    though it were right.

    NOTHING IS DELETED AND NOTHING IS REWRITTEN. The original filing's receipt
    stays exactly as written -- the filing DID happen -- and the reversal is a
    new ``ACTION_UNDO`` row beside it.

    Args:
        provider: The adapter for the mailbox being reversed.
        undo_plan: The plan to execute. Build it with ``plan``.

    Returns:
        The number of messages actually moved back. Fewer than ``len(steps)``
        means at least one provider call was refused; the FAILED rows name them.

    Raises:
        OSError: A ledger write failed. Never swallowed -- a mutation without
            its receipt breaks the audit invariant the kit is sold on, and it is
            the same carve-out ``services/watch/round.py`` makes.
    """
    moved = 0
    for step in undo_plan.steps:
        if step.provider != provider.name:
            # Ids and names only. A row for another account is not an error in
            # the ledger; it is a row this adapter must not act on.
            logger.warning(
                f"UNDO: skipping a row from another provider "
                f"message_id={step.message_id} row_provider={step.provider} "
                f"adapter={provider.name}"
            )
            continue
        moved += _reverse(provider, step)
    return moved


def _reverse(provider: MailProvider, step: Reversal) -> int:
    """Move ONE message back and receipt it. Returns 1 if it moved, 0 if refused.

    Split out of ``apply`` so the broad catch is scoped to exactly one step's
    provider call. Inside the loop it would read as though the ledger append
    were also contained, and that append is the one failure that must propagate.
    """
    provenance = ledger.Provenance(run_id=step.run_id, source_container=step.folder_from)
    try:
        filed = provider.file_message(step.message_id, step.folder_to)
    # BROAD BY DESIGN, exactly as ``round._apply`` is: any refusal from any
    # adapter must become a FAILED receipt and let the reversal continue.
    except Exception as error:  # ruff: ignore[blind-except]
        # DOCUMENTED FAIL-OPEN: the refusal is HANDLED, not swallowed -- the
        # ledger row below records it. This line carries the traceback, which
        # the receipt deliberately does not: the row keeps the exception's TYPE
        # NAME only, because provider prose can quote the subject it refused.
        logger.opt(exception=True).error(
            f"UNDO: message not moved back, receipted FAILED and the reversal "
            f"continues message_id={step.message_id} folder={step.folder_to} "
            f"error_type={type(error).__name__}"
        )
        ledger.append(
            provider.name,
            ledger.ACTION_UNDO,
            step.message_id,
            step.folder_to,
            outcome=ledger.failed(type(error).__name__),
            provenance=provenance,
        )
        return 0
    ledger.append(
        provider.name,
        ledger.ACTION_UNDO,
        ledger.receipt_id_for(filed, step.message_id),
        step.folder_to,
        provenance=provenance,
    )
    return 1
