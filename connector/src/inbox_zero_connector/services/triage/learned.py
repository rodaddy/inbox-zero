"""The learned-rule store: corrections the owner made, kept as ROWS.

WHAT THIS IS
------------
A JSON table of deterministic filing rules that were not hand-written by the
buyer and were not invented by a model. Each row exists because the OWNER moved
a message, and the connector wrote down what that move implied. On the next
sweep the row fires before the model is ever consulted, so the same correction
never has to be made twice.

The file is ``learned-rules.json`` in the connector's config directory, next to
the taxonomy and the connector config, and ``learned_rules_path()`` is the only
place that name is spelled.

THE MIDDLE GROUND -- WHY A ROW AND NOT A PROMPT
----------------------------------------------
There are three ways to make a system remember a correction, and this module
deliberately picks the least fashionable one. The first is to append the
correction to the model's context, so every later classification is made by a
model that has been told about it -- which costs tokens on every message,
degrades as the list grows, and produces an answer nobody can reproduce or
audit. The second is retrieval: embed the corrections, fetch the similar ones,
and hand those to the model. Same non-determinism, plus an index to keep. The
third is this one: a correction becomes a ROW in a small table, the table is
evaluated by ordinary code, and the model is not involved in replaying it at
all. No prompt, no MCP tool, no RAG.

The consequence is the point. A learned decision here is reproducible, is
readable by the owner in a text file, can be disabled by id, and costs nothing
per message. Learning that the model cannot influence is learning that cannot
drift.

THE TRUST MODEL -- WHY ``sender`` IS ADMISSIBLE HERE
---------------------------------------------------
The hand-written ``RULES`` table in ``classify.py`` refuses to route on who
sent a message, because a sender is a claim made BY THE SENDER: anyone can put
any name and any address in a header, so a hand-written rule keyed on it is a
rule an attacker can aim.

A learned rule's value did not come from a sender. It came from the OWNER, who
moved a message into a folder with their own hand. The address is the identity
of the correspondent the owner made a decision ABOUT; the sender asserted
nothing that this table believed. That difference -- owner-chosen versus
sender-asserted -- is the entire reason this table is allowed a predicate the
hand-written one is not.

THE VOCABULARY, AND WHAT IT REFUSES
-----------------------------------
Closed at three predicates, all reading fields the PROVIDER stated:

``sender_is``           exact match on ``config.normalize_address(sender)``
``sender_domain_is``    match on ``config.extract_domain(sender)``
``container_is``        membership in the provider's own container ids

There is no ``subject_contains``, and its absence is not an oversight to be
filled in later -- it is the predicate this design exists to REFUSE. Subject
text and snippet text are written by whoever sent the mail. A rule keyed on
them is a rule a stranger can author by choosing a phrase, and one that fires
on every future message carrying that phrase. Every learned predicate here
reads something the owner chose or the provider stated, and nothing an inbound
message wrote.

Three further refusals, each structural rather than advisory:

* **No rule may target the uncertainty folder.** That folder is where a message
  goes when the connector does NOT know; a learned rule saying "keep sending
  this sender there" is a rule that has learned to keep guessing. "Stop guessing
  at this sender" is a real future feature, and it is a different table than
  this one. WHICH folder holds that role is the TAXONOMY's to declare
  (``taxonomy.review_folder``, issue #159), so the refusal cannot live on the
  row model -- a pydantic validator is handed a row and never a taxonomy, and a
  literal ``"Review"`` check gets a renamed mailbox wrong in both directions at
  once. It lives at the two seams that hold the taxonomy: ``decide.classify``,
  where such a row is skipped and never wins, and ``record_correction``, which
  is handed ``review_folder`` and refuses to mint the row at all. ``load_rules``
  is deliberately NOT one of them -- the watcher loads the table without a
  taxonomy, and a row that cannot fire costs nothing sitting in the file.
* **No expiry.** A row stays until the owner disables it or corrects it. A rule
  that silently ages out re-introduces the surprise the table exists to remove.
* **Only owner corrections write here.** ``record_correction`` is the single
  writer, it is called from the correction path, and there is deliberately no
  MCP tool that lets the AI add a row. The AI can propose a filing; it cannot
  teach itself that the proposal was right.

SHAPE
-----
Module-level functions over a loaded table, matching ``classify``'s functional
style -- ``load_rules`` / ``save_rules`` / ``match`` / ``record_correction`` /
``disable_rule``. Rows are frozen models and every mutator returns a NEW tuple,
so a caller cannot hold a table that disagrees with the file. Timestamps arrive
through a ``now`` argument rather than being read inline, which is what makes
the escalation and correction paths testable without freezing the clock.

``match`` returns a ``LearnedMatch``, not a ``classify.Classification``, and
that is a cycle decision rather than a style one: ``classify`` imports this
module to run the learned pass, so this module importing ``classify`` back
would close the loop. The classify layer builds the ``Classification`` from the
match it is handed.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from inbox_zero_connector.config import config_dir, extract_domain, normalize_address
from inbox_zero_connector.models.mail import Message
from inbox_zero_connector.utils import write_text_atomic

LEARNED_RULES_FILENAME: Final = "learned-rules.json"

SCHEMA_VERSION: Final = 1
"""Bumped when the row shape changes incompatibly. Written on every save."""

ESCALATION_THRESHOLD: Final = 3
"""Address-exact rows on one domain, all naming one folder, before a domain rule.

Three is the smallest count that is a PATTERN rather than a coincidence: two
corrections about one company are plausibly two unrelated messages, and waiting
for four means the owner does the same work a fourth time.
"""

FREE_MAIL_DOMAINS: Final = frozenset({
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
})
"""Domains where the domain is not an identity, so escalation must never fire.

Three corrections about three ``gmail.com`` senders say nothing whatsoever
about the fourth. A domain rule here would file a stranger's mail on the
strength of a shared mail provider, which is the single worst thing this table
could learn.
"""


class Predicate(StrEnum):
    """The closed vocabulary. Each member reads a provider-stated field."""

    SENDER_IS = "sender_is"
    SENDER_DOMAIN_IS = "sender_domain_is"
    CONTAINER_IS = "container_is"


class CorrectionOutcome(StrEnum):
    """What ``record_correction`` did, so the caller can say it out loud."""

    CREATED = "created"
    """No rule matched; a new address-exact row was written."""

    REINFORCED = "reinforced"
    """A rule already said this. Its ``correction_count`` went up, nothing else."""

    CORRECTED = "corrected"
    """A rule said something ELSE. It was disabled and a new row replaced it."""

    ESCALATED = "escalated"
    """A new row was written AND the domain crossed the escalation threshold."""


class LearnedRule(BaseModel):
    """One learned filing rule.

    Frozen, because a rule is a record of a decision the owner made. A caller
    that wants a different rule writes a new row, which is what keeps the file
    on disk and the table in memory from disagreeing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    """Monotonic within the file. Stable, so the owner can disable one by name."""

    predicate: Predicate
    value: str
    """Lowercased at validation, because every field it is compared against is."""

    folder: str
    enabled: bool = True
    learned_at: str
    """ISO timestamp, supplied by the caller's ``now``. Never read inline."""

    source_message_id: str
    """The message whose move taught this. Attribution, for the owner's audit."""

    correction_count: int = 1
    """How many times the owner has made this same correction. Confidence, roughly."""

    disabled_reason: str = ""
    """Why a disabled row is disabled. Empty while enabled."""

    @field_validator("value")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        """Normalise the match value on the way IN, so matching stays a plain ``==``.

        Senders and domains are case-insensitive in practice, and a row learned
        from ``Billing@Example.COM`` must fire on ``billing@example.com``.
        Folding once at construction keeps the evaluator deterministic and
        cheap instead of putting a ``.lower()`` at every comparison site, where
        one omission would make a stored correction silently stop firing.
        """
        return value.strip().lower()


class LearnedMatch(BaseModel):
    """A learned rule fired. The classify layer turns this into a Classification.

    Small and frozen on purpose: this module owes the decision layer the folder,
    the row that decided, and a sentence for the ledger, and owes it nothing
    else.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    folder: str
    rule_id: int
    reason: str = Field(max_length=200)


class _StoreFile(BaseModel):
    """The on-disk envelope. Rows are validated one at a time, not as a list."""

    model_config = ConfigDict(extra="forbid")

    version: int = SCHEMA_VERSION
    rules: list[dict[str, object]] = Field(default_factory=list)


def learned_rules_path() -> Path:
    """Where the learned rules live. The only place the filename is spelled.

    Deliberately here rather than in ``config.py``: this module is
    self-contained, and a caller that wants the path already imports it.
    """
    return config_dir() / LEARNED_RULES_FILENAME


def load_rules(path: Path | None = None) -> tuple[LearnedRule, ...]:
    """Every row, enabled and disabled alike. Callers filter.

    Disabled rows are returned because the owner reviewing the table needs to
    see what was corrected away rather than finding it silently gone. They do
    NOT vote in escalation -- see ``_escalation_domain``.

    Nothing here raises. A missing file is the first run. An unreadable or
    non-object file is announced and treated as empty, on the same reasoning as
    ``watchstate.load``: the recovery is a supported path (the owner corrects
    again), while refusing to start would turn a recoverable state into a
    stopped connector. ONE corrupt ROW is likewise skipped by index rather than
    discarding the rules around it -- a single bad row must not cost the owner
    every correction they ever made.
    """
    target = path or learned_rules_path()
    if not target.exists():
        return ()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        logger.warning(f"LEARNED: rules unreadable, starting fresh path={target}")
        return ()
    if not isinstance(raw, dict):
        logger.warning(f"LEARNED: rules file is not an object, starting fresh path={target}")
        return ()
    try:
        envelope = _StoreFile.model_validate(raw)
    except ValueError:
        logger.warning(f"LEARNED: rules envelope invalid, starting fresh path={target}")
        return ()
    return _validated_rows(envelope.rules, target)


def _validated_rows(rows: list[dict[str, object]], target: Path) -> tuple[LearnedRule, ...]:
    """Validate row by row, naming the index of anything that fails."""
    kept: list[LearnedRule] = []
    for index, row in enumerate(rows):
        try:
            kept.append(LearnedRule.model_validate(row))
        except ValueError as error:
            logger.warning(
                f"LEARNED: skipping corrupt rule row index={index} path={target} error={error}"
            )
    return tuple(kept)


def save_rules(rows: tuple[LearnedRule, ...], path: Path | None = None) -> Path:
    """Write the table atomically. Returns the file written.

    Same posture as ``watchstate.save``: temp file in the SAME directory,
    flushed, ``fsync``ed, then ``replace``d into position, so a crash leaves the
    old complete table or the new complete one and never a half-written file. A
    torn learned-rules file would cost the owner every correction they have ever
    made, which is exactly the loss the corrupt-row tolerance above exists to
    avoid in the first place.
    """
    target = path or learned_rules_path()
    payload = json.dumps(
        {"version": SCHEMA_VERSION, "rules": [row.model_dump(mode="json") for row in rows]},
        indent=2,
        sort_keys=True,
    )
    write_text_atomic(target, payload, what="LEARNED")
    logger.debug(f"LEARNED: rules saved path={target} rule_count={len(rows)}")
    return target


def _matches(row: LearnedRule, address: str, domain: str, containers: tuple[str, ...]) -> bool:
    """Does this row fire? Three provider-stated facts, and nothing else.

    Note the signature. This function is not handed the ``Message``; it is
    handed the three facts the vocabulary is allowed to read. A predicate that
    wanted to consult message text could not be written here without first
    changing this signature, which is the refusal expressed as a type rather
    than as a comment.
    """
    if row.predicate is Predicate.SENDER_IS:
        return bool(address) and address == row.value
    if row.predicate is Predicate.SENDER_DOMAIN_IS:
        return bool(domain) and domain == row.value
    return any(container.lower() == row.value for container in containers)


def match(rows: tuple[LearnedRule, ...], message: Message) -> LearnedMatch | None:
    """The FIRST enabled matching row wins, or None.

    First-match-wins rather than most-specific-wins: the order in the file is
    the order the owner's corrections happened, and a scoring scheme would make
    "why did this fire" a question requiring a debugger instead of a read.
    """
    return match_fields(
        rows,
        sender=message.sender,
        containers=message.containers,
    )


def match_fields(
    rows: tuple[LearnedRule, ...],
    *,
    sender: str,
    containers: tuple[str, ...],
) -> LearnedMatch | None:
    """``match`` over loose fields, for callers that have no ``Message`` to hand.

    ``record_correction`` is one such caller: it is told what the owner moved,
    not handed the header object, and constructing a stand-in ``Message`` just
    to ask this question would mean inventing values for fields no predicate is
    permitted to read.
    """
    address = normalize_address(sender)
    domain = extract_domain(sender)
    for row in rows:
        if row.enabled and _matches(row, address, domain, containers):
            return LearnedMatch(
                folder=row.folder,
                rule_id=row.id,
                reason=(
                    f"Learned rule {row.id}: {row.predicate.value} {row.value} -> {row.folder}"
                ),
            )
    return None


def _next_id(rows: tuple[LearnedRule, ...]) -> int:
    """Monotonic ids, never reused -- a disabled row keeps its number forever."""
    return max((row.id for row in rows), default=0) + 1


def _escalation_domain(rows: tuple[LearnedRule, ...], domain: str, folder: str) -> str | None:
    """The domain to escalate, or None. ENABLED address rows only.

    A disabled row is evidence the owner took that route BACK. Counting it
    lets stale negative evidence mint a domain-wide rule that misfiles every
    new correspondent against the owner's latest correction -- the escalation
    would be built out of decisions they already reversed. Only rows still
    standing may vote. A domain rule that already exists blocks a duplicate.
    """
    if not domain or domain in FREE_MAIL_DOMAINS:
        return None
    already = any(
        row.predicate is Predicate.SENDER_DOMAIN_IS and row.value == domain and row.enabled
        for row in rows
    )
    if already:
        return None
    matching = sum(
        1
        for row in rows
        if row.enabled
        and row.predicate is Predicate.SENDER_IS
        and row.folder == folder
        and extract_domain(row.value) == domain
    )
    return domain if matching >= ESCALATION_THRESHOLD else None


class _Provenance(BaseModel):
    """Which message taught a row, and when. The two fields every new row shares.

    A model rather than two more parameters on ``_new_rule``: the pair travels
    together through every path in this module, and the ceiling on argument
    count is the repo's own (``max-args = 5``) rather than a style opinion.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    now: datetime


def _new_rule(
    rows: tuple[LearnedRule, ...],
    predicate: Predicate,
    value: str,
    folder: str,
    provenance: _Provenance,
) -> LearnedRule:
    """Mint one row, numbering it against the table it is about to join.

    The id comes from ``_next_id(rows)``, so numbering is a function of the
    CURRENT table rather than of a counter kept anywhere else: ids stay
    monotonic and are never reused, which is what lets the ledger and the
    owner's audit refer to a rule by number even after it is disabled. Every
    creation path in this module goes through here so that no row can be born
    without provenance attached.

    Args:
        rows: The table as it stands, read only to derive the next id.
        predicate: What the row matches on.
        value: The match value; ``LearnedRule`` folds it to lowercase.
        folder: Where matching mail is filed.
        provenance: The teaching message id and the caller's ``now``.

    Returns:
        The new row. Not persisted -- the caller writes the table.
    """
    return LearnedRule(
        id=_next_id(rows),
        predicate=predicate,
        value=value,
        folder=folder,
        learned_at=provenance.now.isoformat(),
        source_message_id=provenance.message_id,
    )


# WHY THE SUPPRESSION: five keyword inputs plus the table is what one
# correction IS -- who sent it, where it sat, where the owner put it, which
# message taught it, and when. Collapsing them into a Correction object would
# move the same five fields one level down and add a type the only two callers
# (the CLI and the watcher's correction path) would each have to construct.
# Recorded here rather than raising max-args, on the precedent of
# ``ledger.append``: the ceiling is correct and this one function is the
# exception.
def record_correction(  # ruff: ignore[too-many-arguments]
    rows: tuple[LearnedRule, ...],
    *,
    sender: str,
    containers: tuple[str, ...],
    folder: str,
    message_id: str,
    now: datetime,
    review_folder: str | None = None,
) -> tuple[tuple[LearnedRule, ...], CorrectionOutcome]:
    """The owner moved a message. Write down what that implies.

    Four outcomes, and which one fires is decided by what the table ALREADY
    believes about this message:

    * an enabled rule already matches and names the SAME folder -- the owner has
      told us something we knew, so this is an information update:
      ``correction_count`` goes up and no row is added. Rewriting the row would
      churn the file and lose the original attribution for no gain.
    * an enabled rule matches and names a DIFFERENT folder -- that rule is the
      thing being corrected. It is DISABLED with a reason and KEPT, because the
      owner reviewing this table needs to see what the connector used to think,
      and then the new-rule path runs.
    * nothing matches -- a new address-exact ``sender_is`` row.
    * a new row was written AND this domain now has enough address-exact rows
      naming one folder -- the domain rule is added ALONGSIDE them. The
      address-exact rows stay, so disabling the domain rule later leaves the
      individually-learned senders working.

    ``now`` is a parameter rather than a call, so the timestamps a test asserts
    are the timestamps a test chose.

    ``review_folder`` is how the caller states which folder means "the
    connector does not know" on THIS install. It is a parameter and not a
    constant because the taxonomy owns that name (issue #159) and may have
    renamed it: a literal ``"Review"`` check here refused an ordinary folder
    spelled that way and accepted the real uncertainty folder on a renamed
    mailbox, which is both errors at once. ``None`` means the caller has no
    taxonomy in hand and no refusal is made -- the classify seam still declines
    to let such a row win.

    Args:
        rows: The table as it stands.
        sender: The correspondent the owner made a decision about.
        containers: The provider's own container ids for the message.
        folder: Where the owner put it.
        message_id: The message that taught this, for attribution.
        now: The caller's clock.
        review_folder: The taxonomy's declared uncertainty folder, or ``None``.

    Raises:
        ValueError: The sender is unparseable, or ``folder`` is the uncertainty
            folder -- uncertainty is a state, not a destination a rule may
            launder into fact.
    """
    address = normalize_address(sender)
    if not address:
        raise ValueError(f"correction has an unparseable sender: {sender!r}")
    if review_folder is not None and folder == review_folder:
        raise ValueError(
            f"a learned rule may not target {review_folder!r}: it is this taxonomy's "
            "uncertainty folder, where a message goes when the connector does not know, "
            "and a rule that keeps sending a sender there has learned to keep guessing. "
            "'stop guessing at this sender' is a future feature, not this table."
        )
    hit = match_fields(rows, sender=sender, containers=containers)
    if hit is not None and hit.folder == folder:
        return _reinforced(rows, hit.rule_id), CorrectionOutcome.REINFORCED
    corrected = hit is not None
    if hit is not None:
        rows = disable_rule(rows, hit.rule_id, "corrected by owner")
    rows, outcome = _apply_new_rule(
        rows,
        address=address,
        folder=folder,
        provenance=_Provenance(message_id=message_id, now=now),
    )
    # An escalation is the more informative answer and keeps its name; a plain
    # new row after a disable is reported as the CORRECTION it was, because the
    # caller's message to the owner differs ("learned this" vs "changed this").
    if corrected and outcome is CorrectionOutcome.CREATED:
        return rows, CorrectionOutcome.CORRECTED
    return rows, outcome


def _reinforced(rows: tuple[LearnedRule, ...], rule_id: int) -> tuple[LearnedRule, ...]:
    """Bump one row's ``correction_count``, leaving every other field alone."""
    return tuple(
        row.model_copy(update={"correction_count": row.correction_count + 1})
        if row.id == rule_id
        else row
        for row in rows
    )


def _apply_new_rule(
    rows: tuple[LearnedRule, ...],
    *,
    address: str,
    folder: str,
    provenance: _Provenance,
) -> tuple[tuple[LearnedRule, ...], CorrectionOutcome]:
    """Add the address-exact row, then escalate to a domain row if it is earned."""
    fresh = _new_rule(rows, Predicate.SENDER_IS, address, folder, provenance)
    rows = (*rows, fresh)
    domain = _escalation_domain(rows, extract_domain(address), folder)
    if domain is None:
        return rows, CorrectionOutcome.CREATED
    escalated = _new_rule(rows, Predicate.SENDER_DOMAIN_IS, domain, folder, provenance)
    logger.info(f"LEARNED: escalated to domain rule domain={domain} folder={folder}")
    return (*rows, escalated), CorrectionOutcome.ESCALATED


def disable_rule(
    rows: tuple[LearnedRule, ...], rule_id: int, reason: str
) -> tuple[LearnedRule, ...]:
    """Turn one row off, keeping it and its reason. Unknown id is a ValueError.

    Disabling rather than deleting: the row is the record that the owner once
    made this decision, and a table that silently forgets its own mistakes
    cannot be audited for them.
    """
    known = [row.id for row in rows]
    if rule_id not in known:
        raise ValueError(f"no learned rule with id {rule_id}; known ids: {known}")
    return tuple(
        row.model_copy(update={"enabled": False, "disabled_reason": reason})
        if row.id == rule_id
        else row
        for row in rows
    )
