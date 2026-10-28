"""The per-message decision trace -- append-only JSONL beside the ledger.

One row per message the watch round actually CLASSIFIED. A message that was
out of scope, or that the round had already seen, is never traced, because it
was never read and never classified -- an empty row for it would say nothing
except that the filter worked.

The trace exists for DIAGNOSTICS, and it answers three questions in one line:

1. What the connector SAW -- provider, message id, thread id, the sender's
   DOMAIN, and the folder the message was sitting in.
2. What it DECIDED and why -- the folder, the confidence, the reason, and
   whether a rule rather than a model settled it.
3. What it DID -- the folder the message ended up in, whether the move applied
   or failed, and the provider's error code when it failed.

CONTENT-FREE BY CONSTRUCTION. No subject, no body, no snippet, no sender
address, and no display name reaches this file. The sender arrives already
reduced to a domain, and ``reason`` is truncated at ``MAX_REASON_CHARS``
because a model-produced reason is prose and prose written about a message can
quote the message. The cap is a boundary, not a formatting preference.

THE LEDGER STAYS THE RECEIPT OF RECORD. This file is written alongside it and
never in place of it: nothing here replaces a ledger row, edits one, or excuses
one from being written. A trace row is a diagnostic; a ledger row is evidence.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from loguru import logger
from pydantic import BaseModel, ConfigDict

from inbox_zero_connector.config import extract_domain
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.utils.paths import ledger_path

TRACE_SCHEMA_VERSION: Final = 1
"""The on-disk row shape. Bumped only when a reader of an old row would be wrong."""

MAX_REASON_CHARS: Final = 200
"""The cap on a stored reason, because a model-produced reason is prose.

A rule's reason is a short fixed string; a model's is free text written about
a message it just read, so it is the one field that could carry mail content
back into a content-free file. Truncating is the boundary that makes that
impossible to do accidentally.
"""

TRACE_FILENAME: Final = "decision-trace.jsonl"


class Result(StrEnum):
    """Whether the decided move actually reached the mailbox."""

    APPLIED = "applied"
    """The provider accepted the move and the message is in the new folder."""

    FAILED = "failed"
    """The provider refused. The message did not move."""


def trace_path() -> Path:
    """The trace file: beside the ledger, by construction.

    Derived from ``ledger_path()`` rather than resolving the data directory a
    second time, so there is exactly one place that decides where connector
    data lives. A second resolver is how a test override redirects one file and
    silently misses the other.
    """
    return ledger_path().parent / TRACE_FILENAME


class Subject(BaseModel):
    """What the round SAW about one message, before it decided anything.

    Built by the watcher from the provider's own header projection, never from
    a mail body. ``from_domain`` is a DOMAIN and nothing else -- the local part
    is dropped at the boundary (see ``from_domain``), so no caller can hand a
    full address in and have it land on disk.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    message_id: str
    thread_id: str
    from_domain: str
    folder_before: str


class TraceRow(BaseModel):
    """One classified message: what was seen, what was decided, what happened.

    Frozen, like the ledger's row, because a trace line records an event that
    already occurred. A caller wanting a different row builds a new one, so the
    file and the object can never disagree about what was written.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = TRACE_SCHEMA_VERSION
    timestamp: str
    provider: str
    message_id: str
    thread_id: str
    from_domain: str
    folder_before: str
    folder_after: str
    decision_folder: str
    decision_confidence: float
    decision_reason: str
    deterministic: bool
    result: Result
    error_code: str = ""

    def to_json(self) -> str:
        """One trace row, as the single JSON line appended to the file."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, line: str) -> TraceRow:
        """Parse one stored line back into a row.

        Raises whatever ``json`` or pydantic raises. The trace is a diagnostic
        file, so a caller reading it is already debugging, and a silently
        skipped corrupt line would hide the very thing being looked for.
        """
        row: Any = json.loads(line)
        return cls.model_validate(row)


def from_domain(sender: str) -> str:
    """Reduce a sender to its domain. Never returns the local part.

    Delegates to ``config.extract_domain``, which lowercases, accepts the
    RFC 5322 display form, and returns "" for anything it cannot parse -- so an
    empty or malformed sender yields "" rather than raising. The guard is
    restated here as a return type promise: the caller gets a domain or
    nothing, and there is no third case that leaks an address.
    """
    return extract_domain(sender)


def _now() -> str:
    """The trace timestamp: UTC, ISO-8601, whole seconds.

    Copied from ``ledger._now`` rather than imported: it is private there, and
    the trace must not acquire a dependency on the receipt module's internals
    just to agree with it about a format.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def append(subject: Subject, decision: Classification, *, error_code: str = "") -> TraceRow:
    """Append one diagnostic row for a message that was classified.

    The write is append-mode with an explicit flush and ``os.fsync``, matching
    the ledger: a trace that loses its last rows in a crash is worthless for
    diagnosing the crash.

    ``error_code`` is what decides the whole outcome shape. Non-empty means the
    provider refused, so the message did NOT move and ``folder_after`` is the
    folder it was already in -- recording the decided folder there would claim
    a move that never happened.

    Args:
        subject: What the round saw about the message. Domain only.
        decision: The classification that was produced for it.
        error_code: The provider's own code when the move was refused. Empty
            on success.

    Returns:
        The row that was written.

    Raises:
        OSError: If the row could not be durably written. Never swallowed --
            a diagnostic file that silently stops recording is worse than one
            that is absent.
    """
    target = trace_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    result = Result.FAILED if error_code else Result.APPLIED
    row = TraceRow(
        timestamp=_now(),
        provider=subject.provider,
        message_id=subject.message_id,
        thread_id=subject.thread_id,
        from_domain=subject.from_domain,
        folder_before=subject.folder_before,
        folder_after=subject.folder_before if error_code else decision.folder,
        decision_folder=decision.folder,
        decision_confidence=decision.confidence,
        decision_reason=decision.reason[:MAX_REASON_CHARS],
        deterministic=decision.deterministic,
        result=result,
        error_code=error_code,
    )
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(row.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # Ids and the path only -- the log line obeys the same content-free
        # rule the file does, or a failed write becomes the leak.
        logger.opt(exception=True).error(
            f"TRACE: append failed provider={subject.provider} "
            f"message_id={subject.message_id} path={target}"
        )
        raise
    logger.info(
        f"TRACE: row written provider={row.provider} message_id={row.message_id} "
        f"result={row.result.value} folder_after={row.folder_after}"
    )
    return row


def read_all(path: Path | None = None) -> list[TraceRow]:
    """Read every trace row. A missing file is an empty list, not an error.

    A mailbox no round has classified yet has no trace, which is a normal
    state on a fresh install rather than a fault. Blank lines are skipped so a
    trailing newline does not become a parse failure.
    """
    target = path if path is not None else trace_path()
    if not target.exists():
        return []
    text = target.read_text(encoding="utf-8")
    return [TraceRow.from_json(line) for line in text.splitlines() if line.strip()]
