"""The correction handler -- the body of the ``record_correction`` MCP tool.

WHAT THIS IS
------------
One owner correction, executed. The owner says "this belongs in Receipts", and
two things must follow from that single sentence: the message MOVES, and the
table LEARNS. This module is where those two are done together, in that order,
each with its own receipt.

WHY IT IS A MODULE AND NOT A FUNCTION IN ``server.py``
------------------------------------------------------
``server.py`` is at 461 code lines against this repo's 500-line ceiling
(``scripts/check_code_size.py``), and a correction is not three lines. The
extraction is the ceiling doing its job rather than a preference: the tool
declaration, its wire schema, and its docstring stay in ``server.py`` where the
MCP surface is read as one list, and the behaviour lives here.

INTERACTIVE CAPTURE ONLY -- THE RULE THIS MODULE EXISTS TO KEEP
--------------------------------------------------------------
A learned rule is written by exactly one path in this process: an owner calling
``record_correction`` through the host. Not by the watcher noticing a message
moved, not by the classifier being overruled, not by the model deciding it has
spotted a pattern.

The reason is attribution. A rule inferred from mailbox state cannot tell the
difference between "the owner corrected us" and "the owner was tidying up", or
between either of those and a move the connector itself made a minute earlier --
and a table that learns from its own output is a feedback loop wearing the
costume of learning. An owner correction that arrives through a tool call is an
OBSERVED statement of intent, with a message id attached, at a known time.

That is why ``learned.record_correction`` is called from here and from nowhere
else on the server side. The model layer cannot reach it: there is no tool that
writes a rule directly, and this one requires a message the owner named and a
folder the owner chose.

THE ATTRIBUTION DISCIPLINE
--------------------------
A learned rule MATCHES a message. It did not FILE it -- the connector did,
after the rule matched. The distinction is not pedantry: "filed by your rule"
tells an owner their rule acted on the mailbox autonomously, which is the exact
claim this design refuses to make. Every string this module renders says
"matches", and the tool docstring in ``server.py`` says so too.

Example:
    >>> from inbox_zero_connector.services.triage.corrections import render_rule
    >>> from inbox_zero_connector.services.triage.learned import LearnedRule, Predicate
    >>> rule = LearnedRule(
    ...     id=3,
    ...     predicate=Predicate.SENDER_IS,
    ...     value="partner@acme.example",
    ...     folder="Receipts",
    ...     learned_at="2026-08-19T00:00:00+00:00",
    ...     source_message_id="m-1",
    ... )
    >>> render_rule(rule)
    'rule 3: sender_is partner@acme.example -> Receipts'

See Also:
    - inbox_zero_connector.services.triage.learned: the store, the outcomes, and the escalation
      policy. It owns what a correction MEANS; this module owns when one HAPPENS.
    - inbox_zero_connector.services.audit.ledger: ``ACTION_CORRECTION``, the receipt for a
      change to future behaviour rather than to the mailbox.
    - inbox_zero_connector.apps.server.main: the tool declaration and the MCP surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from inbox_zero_connector.providers import MailProvider, Message
from inbox_zero_connector.services.audit import intents as intents
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.triage import learned as learned
from inbox_zero_connector.services.triage.taxonomy import Taxonomy, load_taxonomy


def render_rule(rule: learned.LearnedRule) -> str:
    """One rule as one sentence the owner can read without a legend.

    Deliberately NOT ``cli._format_rule``. That one is a fixed-width table row
    built for a terminal listing of many rows; this is a single rule shown
    inline in a tool response, where the column padding and the ``corrections=``
    counter are noise. Same facts, different reader.
    """
    return f"rule {rule.id}: {rule.predicate.value} {rule.value} -> {rule.folder}"


def _rule_for(
    rows: tuple[learned.LearnedRule, ...],
    folder: str,
    *,
    sender: str,
    containers: tuple[str, ...],
) -> learned.LearnedRule:
    """The row this correction produced or reinforced, for THIS sender.

    ``learned.record_correction`` returns the whole table and an outcome, not an
    id, because the table is what it owns -- so the row has to be found again
    here.

    THE SENDER IS PART OF THE QUESTION, and leaving it out was a real defect
    rather than a simplification. The first version searched backwards for the
    last enabled row naming the corrected FOLDER, which is right for a creation
    (the new row was just appended) and WRONG for a reinforcement: two senders
    filed to the same folder leave two enabled rows there, so reinforcing
    alice's rule returned bob's -- the owner was told which rule now matches
    their mail and named the wrong one, and the ledger detail recorded the
    wrong ``rule_id`` against the correction.

    Asking ``learned.match_fields`` is asking the store the SAME question the
    store asked itself in ``record_correction``, so the answer cannot disagree
    with the outcome it reported. The backwards folder scan stays as the
    fallback for the case ``match_fields`` cannot answer -- a table whose new
    row does not match its own source header, which the escalation path can
    produce -- and it is reached only when no predicate matches at all.

    Args:
        rows: The table ``record_correction`` returned.
        folder: The folder the owner corrected to.
        sender: The corrected message's sender. The predicate under correction.
        containers: The corrected message's containers, for the same match.

    Raises:
        ValueError: The table came back with no enabled row for this folder,
            which would mean the store and its own writer disagree.
    """
    hit = learned.match_fields(rows, sender=sender, containers=containers)
    if hit is not None and hit.folder == folder:
        for row in rows:
            if row.id == hit.rule_id:
                return row
    for row in reversed(rows):
        if row.enabled and row.folder == folder:
            return row
    raise ValueError(f"correction produced no enabled rule for folder {folder!r}")


def _sender_and_containers(active: MailProvider, message_id: str) -> Message:
    """The header the correction is about, read through an EXISTING provider read.

    ``list_inbox`` rather than a new ``get_message``: widening ``MailProvider``
    is a security-posture change (see ``providers/base.py``), and a correction
    is about a message the owner is looking at right now, which is a message in
    the inbox listing. Adding a method to save one scan would trade the
    connector's smallest surface for a convenience.

    Raises:
        ValueError: No message with that id is in the current inbox listing.
    """
    for message in active.list_inbox(limit=200):
        if message.id == message_id:
            return message
    # message_id= rather than bare prose: the package's redaction pass matches
    # an ATTACHED name, and the enforcement spine fails a content-named
    # interpolation that has no name for a patcher to match on
    # (tests/test_enforcement_spine.py). An id is not content, but the
    # discipline is uniform on purpose -- an exception per safe case is how the
    # rule stops being checkable.
    raise ValueError(
        f"No message_id={message_id!r} in the current inbox listing, so there is nothing "
        "to correct. A correction is recorded for a message the owner can see; "
        "file_message moves one that has already been triaged."
    )


def _file_and_receipt(active: MailProvider, message_id: str, folder: str) -> tuple[str, str]:
    """Move the message and append the FILE receipt. Returns (applied, timestamp).

    Identical in effect to the body of ``server.file_message`` because it is
    now the SAME body: ``intents.file_with_receipt`` owns the ordering (intent
    written, mutation, receipt, intent settled) so a crash between the move and
    its row is reported at the next start instead of vanishing. It also owns the
    post-move id rule -- Graph reassigns the id on a move and returns a
    ``FiledMessage`` carrying the new one; Gmail preserves it and returns a
    plain ``str``, and the key it was given is then the correct receipt.
    """
    applied, entry = intents.file_with_receipt(
        active,
        message_id,
        folder,
        # EXPLICIT, not left to the default. THE OWNER named this folder --
        # this move is the correction itself, not a rule firing. The rule it
        # teaches does not exist yet when this row is written, and filings it
        # settles LATER are the ones that get to count as free. Counting this
        # one would credit the rule layer for the very message that proved it
        # was missing.
        outcome=ledger.Outcome(deterministic=False),
    )
    return str(applied), entry.timestamp


def _refuse_uncertainty_target(folder: str, installed: Taxonomy) -> None:
    """A correction may not teach a rule that files into uncertainty (#168).

    Checked in front of the move, on the same ordering the MCP tool uses for a
    bad folder name: a correction the store will not record must not be
    half-performed. ``file_message`` remains the way to put ONE message in the
    uncertainty folder -- what is refused is teaching a RULE to keep putting
    mail there, which is a rule that has learned to keep guessing.

    The name comes from the TAXONOMY rather than a literal, because the
    taxonomy owns that role (#159) and may have renamed it. A hardcoded
    ``"Review"`` here refused an ordinary folder spelled that way and accepted
    the real uncertainty folder on a renamed mailbox.

    Args:
        folder: Where the owner said the message belongs.
        installed: The closed vocabulary, which declares the uncertainty folder.

    Raises:
        ValueError: When ``folder`` is that declared folder.
    """
    if folder == installed.review_folder:
        raise ValueError(
            f"a correction may not target {folder!r}: it is this taxonomy's uncertainty "
            "folder, where a message goes when the connector does not know, and a rule "
            "that keeps sending a sender there has learned to keep guessing. Use "
            "file_message to move this one message there."
        )


def apply_correction(
    active: MailProvider,
    message_id: str,
    folder: str,
    *,
    installed: Taxonomy | None = None,
) -> dict[str, Any]:
    """Record one owner correction: file the message, then learn from it.

    ORDER IS DELIBERATE. The mailbox move happens first, so a provider refusal
    means no rule is written for a move that did not occur. A rule that outlives
    a failed move would file every future message from that sender on the
    strength of something the owner never actually saw happen.

    TWO ROWS, ONE PER MUTATION. The house rule is one ledger row per mutation,
    and this is two mutations of two different things: the mailbox (``file``)
    and the rule store (``record_correction``). Collapsing them would hide the
    move from the filing count, or hide the rule change from the corrections
    section -- and the digest renders those separately because they answer
    different questions.

    Args:
        active: The live provider. Already resolved by the caller.
        message_id: The message the owner is correcting. Must be validated and
            present in the inbox listing.
        folder: A folder name ALREADY validated against the taxonomy by the
            caller. This function does not re-validate, and must never be the
            first place a folder name is checked.
        installed: The closed vocabulary, supplying the declared uncertainty
            folder a learned rule may not target. Optional and loaded here when
            omitted, so the MCP tool's existing call keeps working; a caller
            that already holds the taxonomy passes it rather than re-reading it.

    Returns:
        The tool response: the file receipt, the rule id, the outcome, the
        rendered rule, and the consent sentence.

    Raises:
        ValueError: The message is not in the inbox listing, the correction
            targets the taxonomy's uncertainty folder, or the store came back
            without an enabled row for this folder.
    """
    vocabulary = installed if installed is not None else load_taxonomy()
    _refuse_uncertainty_target(folder, vocabulary)
    header = _sender_and_containers(active, message_id)
    applied, filed_at = _file_and_receipt(active, message_id, folder)

    rows, outcome = learned.record_correction(
        learned.load_rules(),
        sender=header.sender,
        containers=header.containers,
        folder=folder,
        message_id=message_id,
        now=datetime.now(UTC),
        review_folder=vocabulary.review_folder,
    )
    learned.save_rules(rows)
    rule = _rule_for(rows, folder, sender=header.sender, containers=header.containers)

    entry = ledger.append(
        provider=active.name,
        action=ledger.ACTION_CORRECTION,
        message_id=message_id,
        detail=f"folder={folder} rule_id={rule.id} outcome={outcome.value}",
    )
    return {
        "ok": True,
        "message_id": message_id,
        "folder": folder,
        "applied_as": applied,
        "rule_id": rule.id,
        "outcome": outcome.value,
        "learned_rule": render_rule(rule),
        # "matches", never "filed by". The rule is a test the connector runs
        # before asking a model; it is not an agent that touches the mailbox.
        "what_this_means": (
            f"Recorded. From now on, mail that matches {render_rule(rule)} is filed there "
            "without asking a model. Run 'inbox-zero-connector rules list' to see every "
            "rule, or 'rules disable' to stop this one."
        ),
        "reversible": True,
        "receipt_timestamp": entry.timestamp,
        "filed_receipt_timestamp": filed_at,
    }
