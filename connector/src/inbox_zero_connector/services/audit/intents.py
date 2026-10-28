"""Write-ahead intent journal for mailbox mutations -- JSONL beside the ledger.

THE ORDERING DEFECT THIS CLOSES. Every filing site did the same two steps in
the same order: call the provider, then append the ledger receipt. A crash
between them leaves moved mail with NO row and nothing that knows a row is
owed -- the mailbox changed and the audit trail says it did not, silently.
``ledger.append`` already fsyncs, which makes a written receipt durable; it
cannot make an UNWRITTEN one visible.

So the intent is recorded BEFORE the provider call and settled AFTER the
receipt. A crash anywhere in between leaves an unsettled begin, and the next
daemon start reads it and says out loud which message may have moved without a
row. This journal does not repair anything and must not: the ledger is
append-only evidence, and a process that invents a receipt for a mutation it
did not observe is worse than one that reports a gap it cannot close.

NOT A SECOND LEDGER. Rows here are provisional and the file is REWRITTEN by
``compact`` -- the exact operation the ledger forbids itself. It carries the
same content-free discipline (provider, message id, folder name, timestamp;
never a subject, body, or address), because a file living beside the ledger
inherits the promise that a leak of it is not a leak of the mailbox.

NO FSYNC, DELIBERATELY. A begin that is lost in the same crash that lost the
mutation describes nothing that happened; the durability that matters is the
receipt's, and it already has it. Paying an fsync in front of every provider
call would slow every filing to protect a case with nothing to report.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from loguru import logger

from inbox_zero_connector.services.audit import ledger
from inbox_zero_connector.utils.paths import ledger_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from inbox_zero_connector.providers import MailProvider

INTENTS_FILENAME: Final = "intents.jsonl"


def intents_path() -> Path:
    """The journal file, beside the ledger it guards."""
    return ledger_path().parent / INTENTS_FILENAME


def _now() -> str:
    """The current instant as a UTC ISO-8601 string.

    One helper so every row in this journal carries the same timestamp shape and
    the same clock. A caller reaching for a bare ``datetime.now()`` is how a
    local-time row ends up unsortable against the rest of the file.
    """
    return datetime.now(UTC).isoformat()


def _append_row(row: dict[str, Any]) -> None:
    """Append one JSON object as a line to the intents journal, creating its directory.

    The only writer in this module, so the append mode, the compact separators,
    and the trailing newline that keep the file valid JSONL are decided once.
    Opening per row in append mode is what stops a crash mid-run from truncating
    the intents already recorded.
    """
    target = intents_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def begin(provider: str, message_id: str, folder: str) -> str:
    """Record that a filing is ABOUT to happen. Returns the intent id."""
    intent_id = str(uuid.uuid4())
    _append_row({
        "id": intent_id,
        "ts": _now(),
        "provider": provider,
        "message_id": message_id,
        "folder": folder,
    })
    return intent_id


def settle(intent_id: str) -> None:
    """Record that the filing's receipt was written. The intent is discharged."""
    _append_row({"settled": intent_id, "ts": _now()})


def _read_rows() -> list[dict[str, Any]]:
    """Every parseable row. A malformed line is skipped, never repaired."""
    target = intents_path()
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(f"INTENTS: skipping malformed row path={target}")
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def unsettled() -> list[dict[str, Any]]:
    """Begins with no matching settle -- filings that may hold no receipt."""
    rows = _read_rows()
    settled = {row["settled"] for row in rows if "settled" in row}
    return [row for row in rows if "id" in row and row["id"] not in settled]


def compact() -> None:
    """Rewrite the journal keeping only unsettled begins, so it cannot grow forever.

    Truncating is legal HERE and nowhere near the ledger: a settled intent
    described a mutation whose receipt exists, so the row has no evidentiary
    value once the ledger holds the thing it was guarding.
    """
    remaining = unsettled()
    target = intents_path()
    if not remaining:
        if target.exists():
            target.write_text("", encoding="utf-8")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in remaining),
        encoding="utf-8",
    )


def file_with_receipt(
    active: MailProvider,
    message_id: str,
    folder: str,
    *,
    outcome: ledger.Outcome,
) -> tuple[str, ledger.LedgerEntry]:
    """Move a message and receipt it, with the ordering held in ONE place.

    begin -> ``file_message`` -> ``ledger.append`` -> settle. Every filing site
    calls this rather than spelling the two steps out itself, because three
    copies of an ordering is three chances for one of them to drift.

    Args:
        active: The provider to file through.
        message_id: The id to file.
        folder: The taxonomy folder to file into.
        outcome: The receipt's outcome, stated by the call site -- what makes a
            filing deterministic is local to why it happened, and this seam
            must not decide it for them.

    Returns:
        ``(applied, entry)`` -- whatever ``file_message`` returned, and the row
        that was written. Callers read the post-move id off ``applied``.
    """
    intent_id = begin(active.name, message_id, folder)
    applied = active.file_message(message_id, folder)
    entry = ledger.append(
        provider=active.name,
        action=ledger.ACTION_FILE,
        message_id=ledger.receipt_id_for(applied, message_id),
        detail=folder,
        outcome=outcome,
    )
    settle(intent_id)
    return applied, entry


def reconcile() -> list[dict[str, Any]]:
    """Report every unsettled intent at startup, then compact. Repairs nothing.

    Called once per daemon start. Each row is an ERROR because it names a
    message the mailbox may have moved with no receipt behind it -- the audit
    invariant the kit is sold on, broken in the one place the ledger's own
    fsync cannot reach.

    Returns:
        The unsettled rows that were reported, for a caller that wants to count
        them. Empty on the ordinary clean start.
    """
    pending = unsettled()
    for row in pending:
        logger.error(
            f"INTENTS: filing may have moved mail with NO ledger receipt "
            f"provider={row.get('provider', '-')} "
            f"message_id={row.get('message_id', '-')} "
            f"folder={row.get('folder', '-')} ts={row.get('ts', '-')}"
        )
    compact()
    return pending
