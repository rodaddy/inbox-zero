"""The watch round's AUDIT-WRITING boundary: the ledger row, the trace row, the intent.

WHY THIS IS A SEAM AND NOT A PLACE TO PUT LINES. Every function here talks to
``services.audit`` -- ``ledger``, ``trace``, ``intents`` -- and not one of them
reads a cursor, a page, a scope rule, or a taxonomy. They answer "what does the
record say happened", where ``round`` answers "what should happen to this mail".
The split is the same one ``drafting_loader`` made for the packaging question
(#189 round 7): a module whose whole vocabulary is a different subsystem's is
already a separate module.

IT MOVED HERE WHEN THE RECEIPT GREW A PROVENANCE (#66). ``_receipt`` had to
start carrying a run id and a source container so ``undo`` could reverse a
filing without guessing where it came from, and ``round`` was at exactly the
500-code-line ceiling. Extracting the audit boundary rather than shaving prose
is the correct answer to that: the ceiling is a signal about sprawl, and the
honest reading of it here was that two subsystems were sharing one file.

NOTHING HERE TAKES A ``RoundContext``. The functions take the primitives they
need -- a provider name, a run id, a message -- which is what keeps ``round``
importing this module rather than the two importing each other, and what makes
every one of them callable from a test with no round to build.

Identifiers, folder NAMES, and error CODES only. No subject, no body, no
snippet, and no sender address: the sender is reduced to a domain inside
``_subject`` before it can reach disk, and a provider's error prose is replaced
by its exception TYPE NAME, because prose written about a message can quote the
message.
"""

from __future__ import annotations

from typing import Final

from loguru import logger
from pydantic import BaseModel, ConfigDict

from inbox_zero_connector.providers.base import Message
from inbox_zero_connector.services.audit import intents, ledger, trace
from inbox_zero_connector.services.triage.decide import Classification

UNCLASSIFIED: Final = Classification(
    folder="",
    confidence=0.0,
    reason="classification did not complete",
    deterministic=False,
)
"""The receipt destination for a message whose CLASSIFY step is what failed.

Every FAILED row still names a folder, because ``ledger.append`` takes one --
and a message that never reached a decision has no folder to name. An empty
string says exactly that, and is distinguishable from any real taxonomy folder;
inventing ``Review`` here would put a destination in the audit trail that was
never chosen and never attempted. ``deterministic`` is False for the reason
``append_receipt`` states: nothing was credited because nothing decided.
"""


def subject(
    provider_name: str, message: Message, folder_before: str, message_id: str
) -> trace.Subject:
    """The only place the trace meets the message.

    The sender is reduced to its domain HERE, so no content crosses into audit.
    Every other field is an identifier or a folder name, and the message's
    subject, snippet, and body have nowhere to go from this call.
    """
    return trace.Subject(
        provider=provider_name,
        message_id=message_id,
        thread_id=message.thread_id,
        from_domain=trace.from_domain(message.sender),
        folder_before=folder_before,
    )


def settle(intent_id: str | None) -> None:
    """Discharge the write-ahead intent, if one was ever begun.

    ``None`` means ``classify`` raised before the mailbox was touched, so no
    intent exists and none is owed. Both of ``_apply``'s arms call this after
    their receipt lands -- a FAILED row accounts for the attempt exactly as an
    APPLIED one does, and an intent left behind after either would be reported
    at the next daemon start as a gap that is not there.
    """
    if intent_id is not None:
        intents.settle(intent_id)


class Refusal(BaseModel):
    """WHY a mutation did not happen: the exception, and an override code.

    ONE OBJECT FOR THE SAME REASON ``ledger.Outcome`` IS ONE. The two travel
    together always -- ``error_code`` exists only to override the code derived
    from ``error`` -- and ``append_receipt`` must stay inside the
    5-real-parameter ceiling in ``_DOCS/STANDARDS-python.md`` now that it also
    carries a ``ledger.Provenance``. The parameter count is the signal that
    these are one concept, not two.

    ``NOT_REFUSED`` is the success case: no exception, no code, and
    ``append_receipt`` writes an APPLIED row.

    ``arbitrary_types_allowed`` because ``BaseException`` is not a pydantic
    type. It is held for the length of one call and never validated or
    serialised -- only its TYPE NAME ever reaches disk.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    error: BaseException | None = None
    """The exception a refusal raised. Its TYPE NAME is recorded; its text never is."""

    code: str = ""
    """A code that OVERRIDES the type-name derivation. See ``append_receipt``."""


NOT_REFUSED = Refusal()
"""The default: nothing refused this mutation, so the row is APPLIED."""


def append_receipt(
    provider_name: str,
    message_id: str,
    decision: Classification,
    provenance: ledger.Provenance,
    refusal: Refusal = NOT_REFUSED,
) -> None:
    """Append one ledger row per attempted mutation, applied or refused.

    A FAILURE RECORDS THE EXCEPTION'S TYPE NAME, NEVER ITS TEXT. Provider prose
    can quote the subject line it refused, so the message would carry mailbox
    content into the receipt -- and the ledger's promise is that a leaked ledger
    is not a leaked mailbox (``ledger.error_code``: a code, never a message).

    ``Refusal.code`` OVERRIDES THAT DERIVATION, and it is not a loosening of the
    rule. It exists for the one failure with no exception to name it -- a filing
    abandoned at the attempt bound (``round.RETRY_EXHAUSTED``), where the last
    refusal's type would say "ProviderError" for the fifth time and lose the
    only fact the row is being written to record. The value is a constant the
    round owns, so it is no closer to message content than a type name is; a
    caller passing exception TEXT here would be the violation, and it is the
    same violation as passing it in ``Refusal.error``.

    The action is ``ACTION_FILE`` because the attempted provider mutation is
    exactly ``file_message``. The digest already counts these rows by
    destination folder, so the receipt and the mailbox operation describe the
    same filing without translation.

    ``decision.deterministic`` IS WRITTEN DOWN, and this function is where it
    would otherwise be lost: ``classify`` computes which layer answered, hands
    the whole ``Classification`` here, and before this the receipt kept only the
    folder. The flag is forwarded rather than recomputed -- nothing at this
    boundary knows which layer ran, and a second guess at it here could disagree
    with the one ``classify`` made.

    A REFUSAL CARRIES NO SUCH FLAG. ``ledger.failed`` leaves it at False, which
    is correct on both counts: the mailbox did not change, so there is no filing
    to call free, and crediting the rule layer for work the provider rejected
    would inflate the deterministic share.

    THE PROVENANCE IS CARRIED ON BOTH ARMS, INCLUDING THE REFUSAL (#66), and
    that is deliberate rather than incidental. A FAILED row is not reversible --
    nothing moved -- but recording which run produced it is what lets an
    operator ask "what did this round do" and get the refusals in the answer
    too. ``undo`` selects on ``Status.APPLIED``, so the extra rows cost it
    nothing and hiding them would make a round that refused every message look
    like a round that never ran.

    Args:
        provider_name: ``"google"`` or ``"microsoft"``.
        message_id: The id the receipt names -- the post-move id on success.
        decision: The classification whose folder and determinism are recorded.
        provenance: The run this row belongs to and the container the message
            left. See ``ledger.Provenance``.
        refusal: Why the mutation did not happen, or ``NOT_REFUSED`` when it
            did. See ``Refusal``.
    """
    code = refusal.code or (type(refusal.error).__name__ if refusal.error is not None else "")
    outcome = (
        ledger.Outcome(deterministic=decision.deterministic) if not code else ledger.failed(code)
    )
    ledger.append(
        provider_name,
        ledger.ACTION_FILE,
        message_id,
        decision.folder,
        outcome=outcome,
        provenance=provenance,
    )


class Attempt(BaseModel):
    """What the round knew about one filing at the moment it was refused.

    A model rather than four loose arguments for the reason ``ledger.Outcome``
    is one: they travel together always, and the 5-real-parameter ceiling in
    ``_DOCS/STANDARDS-python.md`` is the signal that they are one concept -- the
    state of a single attempt. ``arbitrary_types_allowed`` because ``Message``
    is held and handed on, never validated or serialised here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    message: Message
    decision: Classification
    folder_before: str
    intent_id: str | None


def record_refusal(
    provider_name: str,
    run_id: str,
    attempt: Attempt,
    error: BaseException,
) -> None:
    """Record ONE message that did not file: log, receipt, settle, trace.

    EXTRACTED FROM ``round._apply``'s except arm, and it is a real unit rather
    than a line-count dodge: every statement here is about accounting for a
    mutation that did NOT happen, where the rest of ``_apply`` is about making
    one happen. It lives in THIS module because every one of those statements is
    an audit write.

    THE ORDER IS FIXED AND MUST NOT BE REARRANGED. The ledger row comes first
    because it is the record of record; the intent is discharged once the
    attempt is accounted for, so a crash before the receipt still reports a gap
    that is really there; the trace is written last because it is diagnostics on
    top of a receipt and must never cost one.

    The ERROR TYPE NAME is what reaches disk, twice and deliberately: as the
    ledger's ``error_code`` and as the trace's. Its TEXT reaches neither, because
    provider prose can quote the subject line it refused.

    Args:
        provider_name: ``"google"`` or ``"microsoft"``.
        run_id: The round this refusal belongs to. See ``ledger.Provenance``.
        attempt: What was known about the filing when it was refused.
        error: The exception the provider or classifier raised.
    """
    logger.opt(exception=True).error(
        f"WATCH: message not filed, receipted as FAILED and the round continues "
        f"folder={attempt.decision.folder or '-'} error_type={type(error).__name__}"
    )
    append_receipt(
        provider_name,
        attempt.message.id,
        attempt.decision,
        ledger.Provenance(run_id=run_id, source_container=attempt.folder_before),
        Refusal(error=error),
    )
    settle(attempt.intent_id)
    trace.append(
        subject(provider_name, attempt.message, attempt.folder_before, attempt.message.id),
        attempt.decision,
        error_code=type(error).__name__,
    )
