"""Append-only audit ledger.

Security invariant 4: everything is receipted. Every mailbox MUTATION appends
exactly one row here, and the append is performed by the connector itself at
the moment the provider call succeeds -- not by the AI, and not as a separate
step the AI could skip, reorder, or lie about.

Two design consequences worth stating plainly, because they are what make the
ledger evidence rather than decoration:

1. There is no MCP tool that writes the ledger. The spec's original tool table
   listed a ``write_ledger`` tool; that was dropped deliberately. A tool the AI
   can call is a tool the AI can decline to call, and an audit trail the
   subject can opt out of is not an audit trail. Writing is a side effect of
   ``file_message`` and ``mark_important`` succeeding.
2. The file is opened "a" (append) and never "w", and nothing in this package
   truncates, rewrites, edits, or deletes a row. Rotation, if it is ever
   wanted, is the owner's own move on their own filesystem.

The ledger records ACTIONS, not mail content. It stores message ids, folder
names, timestamps, and provider names. It deliberately does not store subject
lines, bodies, or addresses: a leaked ledger should not be a leaked mailbox.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from inbox_zero_connector.utils.paths import ledger_path

SCHEMA_VERSION = 5
"""Bumped for the account field: rows name WHICH mailbox the mutation happened in.

One install now watches every account in its configured list (#69, decision
D11), and one ledger receives all of their receipts. Without this field a
two-mailbox install's ledger is a merged stream nothing can split back apart:
``provider`` names ``google`` for both of two Gmail accounts, and the digest,
``undo``, and the morning "what did it do overnight" question all read the two
mailboxes as one. The address is the only value that separates them.

WHY THIS IS A BUMP WHEN ``deterministic`` WAS NOT, on the same test the schema-4
note states: a READER'S CAPABILITY changed. "Which mailbox was this?" is now a
question about the row, and a v1-v4 row's honest answer is "not recorded" --
those rows were written by a watcher that served exactly one mailbox, but the
ledger never said which, so attributing them to the current first account would
be a guess dressed as a receipt. ``_entry_from_row`` reads the key with a
tolerant default and an older row loads as ``None``, which readers must treat
as unknown rather than as the default account.

Additive, exactly as schemas 2, 3, and 4 were. No existing field changes
meaning, nothing is migrated, and a live ledger being appended to right now by
a running watch keeps reading correctly across the change.

THE EARLIER BUMPS, KEPT BECAUSE THEY ARE WHY THE CONTRACT ABOVE IS TRUSTED.

Schema 4 added the reversal fields: rows carry a ``run_id`` and a ``source_container``.

WHY THIS ONE IS A BUMP WHEN ``deterministic`` WAS NOT. The test recorded below
is the PARSER's behaviour, and by that test alone this change would not need a
bump either -- ``_entry_from_row`` reads both new keys with a tolerant default
and an older row loads as ``""``. The bump is here because a READER's
CAPABILITY changed, which is the other half of what this number is for:
``services/audit/undo.py`` selects rows by ``run_id`` and moves mail with
``source_container``, so "is this row reversible" is now a question about the
row, and a v1-v3 row's honest answer is no. Empty is the CORRECT reading of
those rows rather than a defaulted guess -- the receipt boundary recorded
neither value before this schema, so no older row can be shown to name a
source -- and ``undo`` skips them by that emptiness rather than by guessing.

Additive, exactly as schemas 2 and 3 were. No existing field changes meaning,
nothing is migrated, and a live ledger being appended to right now by a running
watch keeps reading correctly across the change.

THE EARLIER BUMPS, KEPT BECAUSE THEY ARE WHY THE CONTRACT ABOVE IS TRUSTED.

Schema 3 added the outcome field: rows carry a ``status`` and an ``error_code``.
Additive only, exactly as schema 2 was. A v1 or v2 row has no ``status`` key;
it reads back as ``Status.APPLIED`` with an empty ``error_code``, and that is
the CORRECT reading rather than a defaulted guess -- before this schema the
ledger recorded successes only, so every row already written IS an applied
mutation. Every older field still means what it meant, so an existing ledger
stays readable rather than being migrated: an audit trail you rewrite is worth
less than one you can only append to.

NO BUMP FOR ``deterministic``. The test is the PARSER's behaviour, not the
newness of the field: ``_entry_from_row`` reads every key with a tolerant
default, so a row lacking ``deterministic`` loads without error and False is
its correct reading (the receipt boundary dropped the flag before the field
existed, so no such row can be shown to have been rule-decided). No existing
field changes meaning, and no reader that understands schema 3 is made wrong by
the new key. That is the same contract ``MUTATING_ACTIONS`` records for adding
a new action VALUE, and it is why a live ledger being appended to RIGHT NOW by
a running watch process keeps reading correctly across this change with no
migration step. A bump would signal a reader-visible break that did not happen,
and the version is only useful if it means that.
"""

SCHEMA_REVERSIBLE: Final = 4
"""The first schema whose rows carry enough to be reversed. See ``SCHEMA_VERSION``.

Named rather than spelled ``4`` at the comparison site in
``services/audit/undo.py``, so the version this capability arrived in is stated
once, beside the version that defines it.
"""

MESSAGE_ID_UNKNOWN: Final = "message-id-unknown"
"""The receipted id when the MUTATION HAPPENED and the provider declined to name it.

Microsoft Graph's ``/move`` is copy-then-delete: it may return ``204 No
Content``, in which case the message in the destination folder exists and has a
new id that this connector was never told. The row is still owed -- the mailbox
changed -- so it is written with this value in place of an id.

CALLERS PASS THIS, NEVER ``None``. ``append`` takes ``message_id: str``, but the
post-move id is reached through an attribute on a value mypy types as ``Any``
(``providers.outlook.provider.FiledMessage.message_id`` is ``str | None``), so a
``None`` slipping through was invisible to the type checker and landed in an
audit row as the literal string ``"None"`` (#169).

AND THE OLD ID IS NOT THE ANSWER EITHER. Falling back to the pre-move id names a
message that was deleted by the move -- a WRONG row that looks exactly like a
right one, which ``reset restore`` cannot tell apart. This value can be told
apart: it is a hyphenated English phrase, outside the alphabet of every id Graph
or Gmail mints, so ``message_id == MESSAGE_ID_UNKNOWN`` is a test rather than a
guess. An honest "the id is unknown" beats a confident lie about which message
moved.
"""

ACTION_FILE = "file_message"
ACTION_IMPORTANT = "mark_important"
ACTION_DRAFT = "create_draft"
ACTION_CORRECTION = "record_correction"
"""An owner correction that mutates the LEARNED-RULE STORE, not the mailbox.

It is receipted because it changes FUTURE behaviour: the next run will file
differently because of it. The owner must be able to audit that exactly like a
move -- a rule that silently rewrites itself is the same trust failure as a
mailbox change with no row.
"""

ACTION_UNDO = "undo"
"""A reversal: a filing this connector made, moved back where it came from.

RECEIPTED LIKE ANY OTHER MUTATION, and that is the point rather than a
formality. An undo is a provider write -- it moves mail -- so it owes a row for
the same reason the filing it reverses owed one, and an audit trail that
recorded the doing but not the undoing would leave the mailbox's real history
unreadable from the file that claims to hold it. The row is APPENDED beside the
filing it reverses; nothing edits or removes that earlier row (``services/audit/
README.md``: a wrong row is corrected by a later row, never rewritten).

Its ``detail`` is the folder the reversal moved the message INTO -- the same
"destination" meaning ``detail`` carries on every other filing row -- and its
``source_container`` is the folder it came out of, which is the original
filing's destination. So a reversal reads as an ordinary filing in the opposite
direction, and ``undo`` selecting on ``run_id`` cannot mistake one for the other
because the action differs.
"""

MUTATING_ACTIONS: frozenset[str] = frozenset({
    ACTION_FILE,
    ACTION_IMPORTANT,
    ACTION_DRAFT,
    ACTION_CORRECTION,
    ACTION_UNDO,
})
"""Every action type that changes state the owner is entitled to audit.

``create_draft`` counts as a mutation even though a draft is not sent: it puts
new content in the owner's mailbox, so it owes a receipt like anything else.

``record_correction`` counts even though it touches nothing in the mailbox at
all. What it changes is the learned-rule store, and a change to the rules is a
change to every filing that comes after it, so it owes a receipt for the same
reason -- the set is "owes a row", not "wrote to the provider".

NO SCHEMA BUMP. ``_entry_from_row`` reads ``action`` as an opaque ``str`` and
validates nothing against this set, so an older reader loads a correction row
without error and every existing field keeps its meaning. The migration
contract in ``SCHEMA_VERSION`` is that rows append and old fields hold; a new
action VALUE on the current shape satisfies both.
"""


class Status(StrEnum):
    """Whether the mutation this row receipts actually reached the mailbox.

    A ``StrEnum`` rather than a bare string or a bool, per
    ``_DOCS/STANDARDS-python.md ## LAW: flat control flow`` -- the set is
    closed, so ``Status.FAILED`` is checked by mypy while a hand-typed
    ``"failed"`` typo would ship silently and read back as an unknown value
    forever. It serialises as its own value, so a row on disk carries
    ``"applied"``, not ``"Status.APPLIED"``.

    THERE ARE EXACTLY TWO STATES, and the absent third is deliberate. A
    "skipped" outcome was considered and left out: a decision that was never
    attempted against the mailbox is not a mutation, and the ledger receipts
    mutations. Nothing in this package would write such a row, and a state no
    caller can produce is a state no reader can trust.
    """

    APPLIED = "applied"
    """The provider call succeeded and the mailbox changed."""

    FAILED = "failed"
    """The action was decided but the provider refused it. The mailbox did NOT
    change, and this row is the only place that fact is recorded."""


class Outcome(BaseModel):
    """What happened to one attempted mutation: the status and its error code.

    ONE object rather than two loose keyword arguments, for two reasons that
    point the same way. First, the two travel together ALWAYS -- an
    ``error_code`` is meaningless without a ``FAILED`` status, and a ``FAILED``
    row without a code cannot be diagnosed. Second, ``append`` must stay inside
    the 5-real-parameter ceiling in ``_DOCS/STANDARDS-python.md``, and the
    parameter count is the signal that these are one concept rather than two.

    ``voice`` is deliberately NOT folded in here. It is passed by name from
    ``server.py``'s draft path, and collapsing it into this object would break
    that call site for a saving this object already delivers.

    ``deterministic`` IS folded in here, for the reason this object exists at
    all: ``append`` sits at the 5-real-parameter ceiling in
    ``_DOCS/STANDARDS-python.md`` with a documented one-over exception, and a
    seventh loose argument would widen that breach rather than carry it. It
    belongs with ``status`` on the merits too -- both describe what the
    connector OBSERVED about one attempted mutation, as opposed to
    ``provider``/``action``/``message_id``/``detail``, which describe the
    mutation itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Status = Status.APPLIED
    """Whether the mutation actually reached the mailbox."""

    error_code: str = ""
    """The provider's own error code or HTTP status. A CODE, never a message.

    Provider prose can quote the subject line it refused, so only the code is
    kept -- the ledger's promise is that a leaked ledger is not a leaked
    mailbox.
    """

    deterministic: bool = False
    """True when a RULE decided this filing, False when a model or the host AI did.

    See ``LedgerEntry.deterministic`` for what makes the value trustworthy.
    The default is False because that is the honest reading of a caller that
    does not say: a filing is only free if something can show a rule settled
    it, and silence is not that showing.
    """


APPLIED = Outcome()
"""The default outcome: the provider call succeeded and the mailbox changed."""


class Provenance(BaseModel):
    """WHERE a mutation came from: which run made it, and out of which container.

    ONE OBJECT FOR THE SAME REASON ``Outcome`` IS ONE. ``append`` already sits
    one over the 5-real-parameter ceiling in ``_DOCS/STANDARDS-python.md`` with
    a documented exception; two more loose arguments would widen that breach
    rather than carry it. The two also belong together on the merits: neither
    describes the mutation (``action``, ``message_id``, ``detail`` do that) and
    neither describes its outcome (``Outcome`` does that). They describe its
    ORIGIN, and a reversal needs both of them or neither -- the run says WHICH
    filings to put back, the container says WHERE each one goes.

    BOTH DEFAULT TO EMPTY, and empty is a real answer rather than a missing one:
    a receipt written by a path that does not belong to a run, or that moved
    mail out of nowhere the caller can name, says so. ``undo`` reads that
    emptiness as "not reversible" and skips the row instead of guessing, which
    is the same refusal the whole module was built around before these fields
    existed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = ""
    """The id of the poll round that produced this row. Empty outside a run.

    Minted ONCE per ``poll_once`` and carried on every receipt that round writes
    (``services/watch/round.py``), so ``undo --run <id>`` selects exactly one
    round's filings. A timestamp alone could not do this: two rounds can start
    in the same second, and rows from a paged round span many.
    """

    source_container: str = ""
    """The container the message was in BEFORE this mutation. Empty when unknown.

    THE FIELD ``undo`` WAS MISSING. ``detail`` records the DESTINATION, so a
    reversal driven off the ledger alone had to guess the origin, and a guess
    that moves mail is the one failure this package exists to make impossible.
    The value is a container NAME from the operator's own taxonomy -- the same
    alphabet ``detail`` already carries -- so it adds no message content to a
    file whose promise is that a leaked ledger is not a leaked mailbox.
    """

    account: str = ""
    """The mailbox address this mutation happened in. Empty when unattributed.

    ORIGIN, WHICH IS WHY IT LIVES HERE rather than beside ``provider``. A
    multi-account install (#69) writes every account's receipts into ONE
    ledger, and ``provider`` cannot separate two Gmail mailboxes -- it says
    ``google`` for both. The address is what a reader splits the stream on.

    An ADDRESS, not a display name and not a message header: it is the value
    the operator typed into the install wizard, already in the config file in
    plain text, so a leaked ledger is not made a leaked mailbox by it.
    """


NO_PROVENANCE = Provenance()
"""The default provenance: this row belongs to no run and names no source."""


def failed(error_code: str) -> Outcome:
    """The outcome for a mutation the provider refused.

    A named constructor rather than a raw ``Outcome(...)`` at each call site,
    so recording a failure without its diagnostic code is awkward to write by
    accident.
    """
    return Outcome(status=Status.FAILED, error_code=error_code)


class LedgerEntry(BaseModel):
    """One receipted mailbox mutation.

    THE ON-DISK KEY IS ``schema`` ON PURPOSE. It is the key on every v1 row
    already written to a real ledger, and this file is append-only by design --
    nothing in this package rewrites a row -- so changing it would strand every
    existing receipt behind a key the reader no longer looks for.

    The ATTRIBUTE is ``schema_version``, because a field literally named
    ``schema`` shadows pydantic's deprecated v1 ``BaseModel.schema()`` and made
    every CLI invocation print a ``UserWarning`` to the operator's terminal.
    ``Field(alias="schema", serialization_alias="schema")`` keeps the two
    independent: the wire format is unchanged in both directions, and the
    warning is gone at its owning boundary rather than filtered downstream.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        protected_namespaces=(),
        ignored_types=(),
        # `schema_version` is constructed by FIELD NAME everywhere in this
        # package and read from disk by its ALIAS. Both must work.
        populate_by_name=True,
    )

    timestamp: str
    provider: str
    action: str
    message_id: str
    detail: str
    voice: str = ""
    """Which voice profile wrote a draft. Empty for non-draft actions.

    Required by the spec: "the ledger records which voice each draft used."
    It records the voice NAME, never the profile text and never the draft
    body, keeping the ledger's promise that a leaked ledger is not a leaked
    mailbox.
    """

    status: Status = Status.APPLIED
    """Whether the mutation reached the mailbox. Defaults to ``APPLIED``.

    The default is what keeps every pre-existing call site correct without
    edits: before schema 3 the ledger recorded successes only, so a row with
    no explicit status IS an applied mutation.
    """

    error_code: str = ""
    """The provider's own error code or HTTP status, for a FAILED row.

    A CODE, never a message and never mail content: ``"403"``,
    ``"ErrorAccessDenied"``, ``"quotaExceeded"``. That is enough to diagnose
    the refusal alongside ``action`` and ``message_id``, and it keeps the
    ledger's promise that a leaked ledger is not a leaked mailbox -- a
    provider's free-text message can quote the subject line it refused.
    Empty on an applied row.
    """

    deterministic: bool = False
    """True when a RULE decided this filing, False when a model or the host AI did.

    THIS IS THE NUMBER THE DETERMINISM-FIRST CLAIM IS SOLD ON. Without it the
    ledger can say what was filed and where, but not which filings were free --
    and after the process exits, the ledger is the only thing left to ask.

    WHY IT CAN BE TRUSTED: it is set by ``classify`` from which layer actually
    answered, never claimed by the layer itself. ``classify`` overwrites what
    an injected classifier reported (``classify.py``: the ``model_copy`` that
    sets ``deterministic`` from ``isinstance(decider, RuleOnlyClassifier)``),
    so a model-produced result cannot describe itself as a rule and land here
    saying so. A row reading True means a hand-written or learned rule matched
    and replaying it consults nothing.

    The default is False, and that is what makes this field additive to a live
    ledger with no migration: a row written before the field existed carries no
    key, reads back False, and False is its CORRECT reading rather than a
    defaulted guess -- the receipt boundary dropped the flag entirely back
    then, so no such row can be shown to have been rule-decided. Claiming those
    rows as free would inflate the exact number the field exists to make
    auditable.
    """

    run_id: str = ""
    """Which poll round wrote this row. See ``Provenance.run_id``.

    Empty on every row written before schema 4, and empty is the CORRECT
    reading: the receipt boundary minted no run id then, so no older row can be
    attributed to a run and inventing one would let ``undo --run`` reverse
    filings it cannot prove belong together.
    """

    source_container: str = ""
    """The container this message left. See ``Provenance.source_container``.

    Empty on every row written before schema 4, and empty is what makes those
    rows honestly unreversible rather than dangerously reversible -- see
    ``SCHEMA_VERSION``.
    """

    account: str | None = None
    """Which mailbox this mutation happened in. See ``Provenance.account``.

    ``None`` on every row written before schema 5, and None means UNKNOWN
    rather than "the first account": those rows were written by a
    single-mailbox watcher that never recorded which mailbox it was, so naming
    one now would be a guess -- and a reader splitting a merged ledger on a
    guess attributes one mailbox's filings to another.

    OPTIONAL RATHER THAN EMPTY-STRING, unlike the schema-4 fields beside it,
    because here the two states differ. ``run_id=""`` and
    ``source_container=""`` mean "not reversible" whichever way the row got
    that way, so one value serves both. An address is a KEY a reader groups
    on: a row a multi-account watcher wrote with no address (``""``) and a row
    predating the field (``None``) must not land in the same bucket, and a
    ``None`` cannot be compared equal to any real address by accident.
    """

    # WHY THE ALIAS: the on-disk key is `schema` on every row of a real
    # append-only ledger (see the class docstring), but an ATTRIBUTE of that
    # name shadows pydantic's deprecated v1 `BaseModel.schema()` and emits a
    # `UserWarning` on every import -- which reached the operator's terminal on
    # every CLI invocation. The alias keeps the wire format byte-identical
    # while the attribute carries a name that shadows nothing.
    schema_version: int = Field(
        SCHEMA_VERSION,
        # Read rows keyed `schema`; write them back the same way. The field
        # NAME stays available as a constructor kwarg via `populate_by_name`,
        # which is how every call site in this package builds an entry.
        validation_alias="schema",
        serialization_alias="schema",
    )

    def to_json(self) -> str:
        """One ledger row, as the single JSON line that is appended to the file."""
        # `by_alias` is what puts the `schema` key back on the wire; without it
        # the row would ship the internal `schema_version` name and every
        # existing reader of this ledger would stop finding the version.
        return json.dumps(self.model_dump(mode="json", by_alias=True), sort_keys=True)


def _now() -> str:
    """The receipt timestamp: UTC, ISO-8601, whole seconds.

    UTC rather than local time because a ledger read months later must not
    depend on the reader's timezone, and second precision because
    ``entries_for_day`` selects rows by an ``isoformat()`` date PREFIX -- a
    stable, sortable, prefix-comparable string is what makes that string match
    correct without parsing every row.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


# WHY THE NOQA: `append` sat at exactly the 5-real-parameter ceiling before the
# outcome field existed, so recording an outcome at all breaches PLR0913 by one.
# `status` and `error_code` are already collapsed into a single `Outcome` object
# -- that is what keeps this at 6 rather than 7. Going lower means folding
# `provider`/`action`/`message_id`/`detail` into a mutation object, which is the
# right shape and is a change to `server.py`'s three call sites; that file is
# owned by another lane right now. Recorded here rather than raising `max-args`,
# because the ceiling is correct and this one function is the exception.
def append(  # ruff: ignore[too-many-arguments]
    provider: str,
    action: str,
    message_id: str,
    detail: str = "",
    *,
    voice: str = "",
    outcome: Outcome = APPLIED,
    provenance: Provenance = NO_PROVENANCE,
) -> LedgerEntry:
    """Append one receipt. Called by the connector, never by the host AI.

    The write is append-mode with an explicit flush and ``os.fsync``: a crash
    between the provider call and the receipt would otherwise leave a mutation
    with no row, which is the one failure mode that makes the ledger unusable
    as evidence.

    ``voice`` and ``outcome`` are KEYWORD-ONLY. Positionally they read as more
    interchangeable values after ``detail``, and adjacent arguments are easy to
    swap in a call nobody re-reads.

    ``outcome`` DEFAULTS to ``APPLIED``, which is what makes this change
    additive: every existing call site records a success exactly as it did
    before. A failure is opt-in and explicit, because a classification that was
    decided and then refused is a state the operator needs to see -- silently
    dropping it, or recording it as applied, are the two wrong answers this
    field exists to remove.

    There is NO ``path`` parameter. The write target comes from the settings
    keystone, so there is exactly one ledger per deployment and no call site
    can redirect a receipt somewhere the digest will not read it. Tests
    redirect the whole data directory instead, which is the same override the
    operator has.

    Args:
        provider: ``"google"`` or ``"microsoft"``.
        action: One of ``MUTATING_ACTIONS``.
        message_id: The provider's own id for the message or thread, naming
            the message as it exists AFTER the mutation. Never ``None`` and
            never empty: when the provider performed the mutation without
            reporting the resulting id, pass ``MESSAGE_ID_UNKNOWN``.
        detail: Folder name, flag state, or ``draft:<id>``.
        voice: Which voice profile wrote a draft. Empty for other actions.
        outcome: What the connector observed about this mutation: whether it
            reached the mailbox, the provider's error code when it did not, and
            whether a RULE rather than a model decided it. ``APPLIED`` unless
            the caller is receipting a refusal or a deterministic filing; build
            one with ``ledger.failed(code)`` or
            ``ledger.Outcome(deterministic=True)``.
        provenance: Where this mutation came from: the run that produced it,
            the container the message left, and the mailbox it happened in.
            ``NO_PROVENANCE`` unless the caller can name them -- an empty field
            is what tells ``undo`` the row is not reversible, and a guess there
            would move mail to the wrong folder with a receipt saying it was
            right.

    Returns:
        The row that was written.

    Raises:
        ValueError: If ``message_id`` is empty or whitespace. A row naming no
            message is not a receipt, and recording one would make an unknown
            id indistinguishable from a real one.
        OSError: If the row could not be durably written. Never swallowed -- a
            mutation without its receipt breaks the audit invariant the kit is
            sold on.
    """
    # CHECKED BEFORE THE DIRECTORY IS CREATED, so a refused append leaves no
    # trace at all. `message_id` is declared `str`, but every caller reaches the
    # post-move id through an attribute on an `Any` (see `MESSAGE_ID_UNKNOWN`),
    # which is exactly the path mypy cannot see -- so the type alone is not the
    # guard. Pydantic already rejects `None` below; this rejects the empty and
    # whitespace ids it accepts, because a row naming nothing is not a receipt.
    if not message_id or not str(message_id).strip():
        raise ValueError(
            f"message_id is required for a {action!r} receipt and was empty. "
            f"A mutation whose resulting id the provider declined to name is "
            f"receipted with ledger.MESSAGE_ID_UNKNOWN, never with None or ''."
        )
    target = ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = LedgerEntry(
        timestamp=_now(),
        provider=provider,
        action=action,
        message_id=message_id,
        detail=detail,
        voice=voice,
        status=outcome.status,
        error_code=outcome.error_code,
        deterministic=outcome.deterministic,
        run_id=provenance.run_id,
        source_container=provenance.source_container,
        account=provenance.account,
        # WHY EXPLICIT: `Field(...)` hides the default from mypy (this repo runs
        # no pydantic mypy plugin), and a new row is always written at the
        # CURRENT schema version, so stating it costs nothing and is accurate.
        schema_version=SCHEMA_VERSION,
    )
    _write_row(entry, target)
    return entry


def _write_row(entry: LedgerEntry, target: Path) -> None:
    """Append one row durably, or raise. The fsync half of ``append``.

    SPLIT OUT OF ``append``, WHICH IS AT THE PER-FUNCTION CODE-LINE CEILING
    (``scripts/check_code_size.py``) -- and the seam is a real one rather than
    a line-count dodge: everything above it decides WHAT the row says and
    refuses a row that says nothing, everything here is durability. The
    refusal, the fsync, and the two log lines are unchanged.

    Raises:
        OSError: The row could not be durably written. Never swallowed -- a
            mutation without its receipt breaks the audit invariant the kit is
            sold on.
    """
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(entry.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # A mutation without its receipt is the one failure that makes the
        # ledger unusable as evidence, so it is loud and it is fatal to the
        # call. Never swallowed, never a silent no-op.
        logger.opt(exception=True).error(
            f"LEDGER: append failed provider={entry.provider} action={entry.action} "
            f"message_id={entry.message_id} path={target}"
        )
        raise
    logger.info(
        f"LEDGER: receipt written provider={entry.provider} action={entry.action} "
        f"message_id={entry.message_id} voice={entry.voice or '-'} "
        f"status={entry.status.value} error_code={entry.error_code or '-'} "
        f"deterministic={entry.deterministic}"
    )


def receipt_id_for(filed: str, requested_id: str) -> str:
    """Which id a FILE receipt names, given what ``file_message`` returned.

    ONE FUNCTION FOR BOTH CALL SITES because the rule is one rule, and it was
    previously spelled out as a bare ``getattr`` at each of them -- which is how
    they came to disagree about the ``None`` case (#169). The seam belongs here
    rather than at either caller: the ledger owns what a receipt is allowed to
    say, and ``MESSAGE_ID_UNKNOWN`` is its constant.

    Three provider shapes, three answers:

    * A plain ``str`` (Gmail). Labelling preserves the id, so the id the caller
      passed in still resolves and IS the answer.
    * A ``FiledMessage`` naming a post-move id (Graph). Graph's ``/move`` is
      copy-then-delete, so that new id is the only one that resolves.
    * A ``FiledMessage`` whose id is ``None`` (Graph, ``204 No Content``). The
      move happened and the resulting id is unknowable from here, so the row
      says ``MESSAGE_ID_UNKNOWN`` -- not ``None``, and NOT ``requested_id``,
      which the move already deleted.

    ``getattr`` rather than an ``isinstance`` check on ``FiledMessage``: this
    package's provider-neutral layers do not import a specific adapter, and the
    declared return type of ``MailProvider.file_message`` is ``str``. The
    annotations here are what the old inline spelling lacked -- the value is
    narrowed to ``str`` before it leaves, so a ``None`` can no longer reach
    ``append`` behind an ``Any``.

    Args:
        filed: Whatever ``MailProvider.file_message`` returned.
        requested_id: The id the caller asked to have filed.

    Returns:
        The id to record, guaranteed non-empty.
    """
    post_move: str | None = getattr(filed, "message_id", requested_id)
    if post_move is None:
        # The provider moved the message and did not say where it landed. The
        # requested id is DEAD (copy-then-delete), so naming it would be a
        # wrong row wearing the shape of a right one.
        return MESSAGE_ID_UNKNOWN
    return post_move or MESSAGE_ID_UNKNOWN


def read_all(path: Path | None = None) -> list[LedgerEntry]:
    """Read every receipt. Malformed lines are skipped, never repaired in place."""
    return list(_iter_entries(path))


def _iter_entries(path: Path | None = None) -> Iterator[LedgerEntry]:
    """Stream the ledger one row at a time, announcing every row it cannot read.

    A generator over the open file rather than a read-then-parse: the ledger
    only grows, and a digest must not need the whole file resident to count a
    day's rows. A missing file yields nothing -- a mailbox that has never been
    mutated has no receipts, which is not an error.

    Three rejection points (undecodable JSON, a non-object row, a row whose
    fields will not validate) all SKIP and log at WARNING rather than raising.
    One corrupt line must not take down a digest, and a silent drop would
    under-report what was done to the mailbox -- the one defect this file
    exists to prevent. Only the line NUMBER is logged, never the line: a
    corrupt row may hold anything.

    Args:
        path: Ledger file to read. Defaults to ``ledger_path()``; the override
            exists for tests and for auditing a rotated copy.

    Yields:
        Each readable receipt, in file order.
    """
    target = path or ledger_path()
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as handle:
        for number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # FALLBACK point: a row is skipped, not repaired in place, and
                # the skip is announced. A digest that silently drops rows
                # under-reports what was done to the mailbox, which is the one
                # thing this file exists to prevent. The line NUMBER is logged,
                # never its content -- a corrupt row may hold anything.
                logger.warning(
                    f"LEDGER: skipping unparseable row path={target} line_number={number}"
                )
                continue
            if not isinstance(raw, dict):
                logger.warning(
                    f"LEDGER: skipping non-object row path={target} line_number={number}"
                )
                continue
            entry = _entry_from_row(raw)
            if entry is None:
                # Same FALLBACK contract as an unparseable line above: the row
                # is skipped and announced, never coerced. Reading an
                # unrecognised outcome as APPLIED would turn a corrupt row
                # into a false claim that the mailbox changed.
                logger.warning(
                    f"LEDGER: skipping row with unreadable fields "
                    f"path={target} line_number={number}"
                )
                continue
            yield entry


def _entry_from_row(raw: dict[str, Any]) -> LedgerEntry | None:
    """Build one entry from a decoded row, or ``None`` if it cannot be read.

    Returns ``None`` rather than raising so that a single corrupt row cannot
    take down a whole digest -- the caller announces the skip. Field defaults
    here are what make an OLD ledger readable: a v1/v2 row carries no
    ``status``, and APPLIED is its true reading because the ledger recorded
    successes only before schema 3.
    """
    try:
        return LedgerEntry(
            timestamp=str(raw.get("timestamp", "")),
            provider=str(raw.get("provider", "")),
            action=str(raw.get("action", "")),
            message_id=str(raw.get("message_id", "")),
            detail=str(raw.get("detail", "")),
            # Absent on every v1 row; "" is the correct reading, not a
            # defect, because v1 could not create drafts at all.
            voice=str(raw.get("voice", "")),
            # Absent on every v1 and v2 row. APPLIED is the correct reading
            # rather than a defaulted guess: before schema 3 the ledger
            # recorded successes only, so an older row IS applied.
            status=Status(str(raw.get("status", Status.APPLIED.value))),
            error_code=str(raw.get("error_code", "")),
            # Absent on every row written before this field existed. False is
            # the correct reading, not a defaulted guess: the receipt boundary
            # dropped the flag back then, so no older row can be shown to have
            # been rule-decided, and reading one as free would inflate the
            # number this field exists to make auditable.
            # An absent key gives None, and `bool(None)` is the False this
            # comment describes; the fallback is left implicit because ruff
            # (falsy-dict-get-fallback) reads a spelled-out falsy default as
            # noise, and both spellings produce the identical value.
            deterministic=bool(raw.get("deterministic")),
            # Absent on every row written before schema 4. "" is the correct
            # reading, not a defaulted guess: the receipt boundary recorded
            # neither value then, so an older row names no run and no source --
            # and `undo` refuses such a row rather than guessing where its
            # message came from. This is what "old rows load with None" means
            # in practice at a field typed `str`.
            run_id=str(raw.get("run_id", "")),
            source_container=str(raw.get("source_container", "")),
            # Absent on every row written before schema 5, and None -- not ""
            # -- is its correct reading. The two are different answers here:
            # "" is a row a multi-account watcher wrote without an address,
            # None is a row from a watcher that had no concept of one. A
            # reader splitting a merged ledger by mailbox must not fold the
            # second into the first account, so the absent case is typed so it
            # cannot be compared equal to any address by accident.
            account=None if raw.get("account") is None else str(raw["account"]),
            schema_version=int(str(raw.get("schema", SCHEMA_VERSION))),
        )
    except ValueError, TypeError:
        # DOCUMENTED FAIL-OPEN. Returning None rather than raising is this
        # function's contract -- one corrupt row must not take down a whole
        # digest -- and `_iter_entries` announces the skip at WARNING with the
        # line number.
        #
        # This line is NOT a duplicate of that one. The caller can only say
        # THAT a row was unreadable; it never sees the exception, so the
        # WHICH-FIELD is lost exactly when someone is trying to work out why
        # rows are being dropped. DEBUG because the operator-facing warning is
        # already emitted upstream; this carries the cause for whoever turns
        # the level up to diagnose it.
        logger.opt(exception=True).debug(
            "LEDGER: row rejected by field validation (caller reports the skip and its line number)"
        )
        return None


def entries_for_day(day: date, path: Path | None = None) -> list[LedgerEntry]:
    """Every receipt written on ``day``, in file order.

    Selection is a string PREFIX match on the stored timestamp, not a parse
    into ``datetime``: ``_now`` writes a UTC ISO-8601 string, so
    ``day.isoformat()`` is exactly its first ten characters, and comparing
    strings avoids re-parsing every row in the file to answer a question about
    one day. The consequence worth knowing is that ``day`` is a UTC day --
    the ledger's own clock -- not the reader's local one.

    Args:
        day: The UTC calendar day to select.
        path: Ledger file to read. Defaults to ``ledger_path()``.

    Returns:
        The matching receipts; empty when nothing was done that day.
    """
    prefix = day.isoformat()
    return [entry for entry in _iter_entries(path) if entry.timestamp.startswith(prefix)]


def daily_digest(day: date | None = None, path: Path | None = None) -> str:
    """Render a plain-text summary of one day's mailbox activity.

    This is what the owner reads to answer "what did it do to my mailbox
    today?". It counts filings per folder and flags raised, and it names the
    ledger file so the owner can audit the raw rows.
    """
    target_day = day or datetime.now(UTC).date()
    entries = entries_for_day(target_day, path)
    ledger_file = path or ledger_path()
    logger.debug(
        f"LEDGER: digest in day={target_day.isoformat()} "
        f"entry_count={len(entries)} path={ledger_file}"
    )

    header = f"Inbox Zero -- mailbox activity for {target_day.isoformat()}"
    if not entries:
        return f"{header}\n\nNo mailbox changes were made.\n\nLedger: {ledger_file}"

    # A refused action changed nothing, so it is counted separately from the
    # first line onward. Folding failures into the "filed" count would make
    # the digest overstate the mutation, which is the one defect the ledger
    # exists to prevent.
    applied = [entry for entry in entries if entry.status is Status.APPLIED]
    refused = [entry for entry in entries if entry.status is Status.FAILED]
    providers = sorted({entry.provider for entry in entries})

    lines = [
        header,
        "",
        f"{len(applied)} change(s) across: {', '.join(providers)}",
        "",
    ]
    for section in (
        _filed_section,
        _flagged_section,
        _refused_section,
        _drafted_section,
        _corrections_section,
    ):
        lines.extend(section(applied, refused))
    lines.extend([
        "Every mailbox change above is a reversible label/move/flag, or a draft",
        "waiting in your Drafts folder. Nothing was sent, deleted, or forwarded --",
        "this connector has no such capability. You are the only sender.",
    ])
    # Said only on a day that HAS one. The reversibility sentence above covers
    # mailbox changes, and a rule correction is not one -- it changed what
    # happens next, not what is in the mailbox. Stating that unconditionally
    # would put the word "correction" in every digest, including the days
    # nothing was corrected, which is the kind of boilerplate an owner learns
    # to skip.
    if any(entry.action == ACTION_CORRECTION for entry in applied):
        lines.append("Rule corrections changed no mail -- run 'rules --list' to see them.")
    lines.extend([
        "",
        f"Ledger: {ledger_file}",
    ])
    return "\n".join(lines)


def _filed_section(applied: list[LedgerEntry], refused: list[LedgerEntry]) -> list[str]:
    """Filings per folder, with the deterministic split. Only APPLIED rows.

    A refusal moved nothing, so it is counted in neither total -- including the
    free/paid split, where counting a refused rule-decided filing as "free"
    would credit the rule layer with work the mailbox never saw.

    THE SPLIT IS ON THE HEADLINE LINE rather than in a section of its own,
    because it is a property of the filings already being reported and an owner
    reading "Filed 12" should not have to find a second line to learn what it
    cost. "by rule (free)" says the consequence, not the mechanism: the point
    the owner is being asked to care about is that those filings consumed no
    model call.
    """
    filings = [entry for entry in applied if entry.action == ACTION_FILE]
    if not filings:
        return []
    filed = Counter(entry.detail for entry in filings)
    by_rule = sum(1 for entry in filings if entry.deterministic)
    by_model = len(filings) - by_rule
    return [
        f"Filed {len(filings)} message(s): {by_rule} by rule (free), {by_model} by model.",
        *(f"  {folder}: {count}" for folder, count in sorted(filed.items())),
        "",
    ]


def _flagged_section(applied: list[LedgerEntry], refused: list[LedgerEntry]) -> list[str]:
    """Flags actually raised on the mailbox."""
    flagged = [entry for entry in applied if entry.action == ACTION_IMPORTANT]
    if not flagged:
        return []
    return [f"Marked important: {len(flagged)} message(s)", ""]


def _refused_section(applied: list[LedgerEntry], refused: list[LedgerEntry]) -> list[str]:
    """Actions the provider refused, named with their codes.

    An operator reading this needs to know WHAT was refused and WHY, and those
    two facts are the entire diagnosis surface a receipt is allowed to carry.
    The section is absent on a clean day: an empty heading trains people to
    skip the one that is not empty.
    """
    if not refused:
        return []
    counts = Counter(f"{e.action} ({e.error_code or 'no code'})" for e in refused)
    return [
        f"{len(refused)} action(s) were refused and did NOT change the mailbox:",
        *(f"  {what}: {count}" for what, count in sorted(counts.items())),
        "",
    ]


def _drafted_section(applied: list[LedgerEntry], refused: list[LedgerEntry]) -> list[str]:
    """Drafts created, broken down by voice.

    The voice breakdown is the owner's check on the routing table: if drafts
    to customers are coming out in the internal voice, this line is where they
    see it, without opening a single draft.
    """
    drafted = [entry for entry in applied if entry.action == ACTION_DRAFT]
    if not drafted:
        return []
    by_voice = Counter(entry.voice or "(unrecorded)" for entry in drafted)
    return [
        f"Reply drafts created: {len(drafted)} (NOT sent)",
        *(f"  voice {name}: {count}" for name, count in sorted(by_voice.items())),
        "",
    ]


def _corrections_section(applied: list[LedgerEntry], refused: list[LedgerEntry]) -> list[str]:
    """Learned-rule corrections, one line each.

    PER ROW rather than a count per folder, unlike ``_filed_section``. A filing
    is one of many identical events and only the total is interesting; a
    correction changes the rules, so which rule changed and how is the whole
    point of showing it at all. Two corrections to the same folder are two
    different facts.

    The line carries the message that triggered the correction, the rule it
    produced, and what happened to that rule -- a receipt ABOUT A RULE. No
    subject and no body, keeping the ledger's promise that a leaked ledger is
    not a leaked mailbox.

    Absent on a day with none, matching every other section here: an empty
    heading trains people to skip the one that is not empty.
    """
    corrections = [entry for entry in applied if entry.action == ACTION_CORRECTION]
    if not corrections:
        return []
    return [
        f"Learned-rule corrections recorded: {len(corrections)} (no mailbox change)",
        *(f"  {entry.message_id}: {entry.detail}" for entry in corrections),
        "",
    ]


def thread_has_draft(thread_id: str, path: Path | None = None) -> bool:
    """True when this connector has ALREADY drafted on ``thread_id``.

    THE IDEMPOTENCY ORACLE FOR ``create_draft``, and the reason there is no
    second state file. ``create_draft`` is not idempotent at the provider: a
    replayed round would leave a second draft in the owner's mailbox and a
    second receipt for one real intent. The ledger is already the record of
    every mutation this connector performed, it is append-only, and NOTHING
    clears it -- in particular a cursor resync does not, which is exactly the
    case a ``seen``-window guard cannot cover (``watchstate.SEEN_LIMIT`` is a
    bounded window and a message falling outside it is reprocessed as new).

    ONE DRAFT PER THREAD, FOREVER -- operator ruling, 2026-08-19. Not "one per
    round" and not "one per window". A row here is permanent, so this answer
    only ever moves from False to True.

    ============ ITS HONEST WEAKNESS, WRITTEN DOWN RATHER THAN HIDDEN =========

    THE LEDGER KNOWS WHAT THIS CONNECTOR DRAFTED. IT DOES NOT KNOW WHAT IS IN
    THE MAILBOX. An owner who deletes a draft has left a row behind, so this
    returns True and the thread is never drafted again.

    That is the CORRECT default rather than a defect to fix: a draft the owner
    deleted is a draft they did not want, and re-creating it would argue with
    them silently and repeatedly. The alternative -- asking the provider
    whether a draft still exists -- would make a network call per candidate
    thread and would read "deleted" as "please try again", which is the
    reading the owner's action contradicts.

    A ``Status.FAILED`` row counts. The attempt reached the provider boundary
    and its outcome is not knowable from here; re-attempting on the strength of
    a recorded failure is how one contended thread becomes a retry loop that
    eventually lands two drafts.

    Args:
        thread_id: The thread key, exactly as ``create_draft`` receipts it
            (``server.py`` passes ``thread_key`` as ``message_id``).
        path: Ledger override. Tests redirect the whole data directory.

    Returns:
        True when any ``ACTION_DRAFT`` row names this thread.
    """
    key = thread_id.strip()
    if not key:
        return False
    return any(
        entry.action == ACTION_DRAFT and entry.message_id == key for entry in _iter_entries(path)
    )
