"""What a DRY round decided, and the decision that produces it (#66).

A SEPARATE MODULE FROM ``round.py`` FOR THE REASON THE SIZE LAW EXISTS. The
round module is the mutation engine -- cursors, retries, receipts, intents,
replay marks -- and this is the one path that deliberately performs none of
them. Keeping the preview beside the machinery it refuses to run put
``round.py`` over the code-line ceiling in
``_DOCS/STANDARDS-python.md``, and the honest fix is the split rather than a
larger ceiling: a reader auditing "what can this connector write" should not
have to read the branch that writes nothing.

THE PREVIEW CARRIES IDENTIFIERS ONLY. ``PlannedFiling`` is printed to a
terminal by ``apps/cli/dry_run_preview.py`` and pasted into support threads, so
the rule ``utils/logging_config.py`` states for the log sinks -- never a body,
subject, address, or token -- applies to it unchanged. The model forbids extra
fields, so a future caller cannot attach message content to it either.
"""

from __future__ import annotations

from loguru import logger
from pydantic import BaseModel, ConfigDict

from inbox_zero_connector.providers.base import Message
from inbox_zero_connector.services.triage.decide import Classification, Classifier, classify
from inbox_zero_connector.services.triage.learned import LearnedRule
from inbox_zero_connector.services.triage.taxonomy import Taxonomy


class PlannedFiling(BaseModel):
    """One filing a DRY round decided on and deliberately did not perform.

    IDENTIFIERS ONLY, AND THAT IS THE SAME SECURITY PROPERTY ``RoundSummary``
    ENFORCES. A subject or sender field here would put privileged mail on a
    screen and in a paste buffer -- the identical leak the logging rule closes,
    arriving through stdout instead. There is no field on this model that can
    hold message text, and the folder name is the operator's OWN taxonomy
    rather than anything the mail said.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    """The provider's id for the message. Not its subject and not its sender."""

    folder: str
    """The taxonomy folder ``classify`` chose. A name from the installed
    taxonomy, so it is the operator's own vocabulary, never the message's."""

    deterministic: bool
    """True when a hand-written or learned RULE settled the decision, false when
    the classifier did. Carried because it is what tells an operator reading the
    preview which planned filings would be identical on the real pass and which
    depend on a model that may answer differently."""


def plan(message: Message, decision: Classification) -> PlannedFiling:
    """Project one already-made decision onto the preview record.

    A FUNCTION RATHER THAN A CONSTRUCTOR CALL AT THE CALL SITE, because this is
    the seam where message content could leak in: it takes the whole
    ``Message`` and the whole ``Classification`` and deliberately narrows them
    to three fields. A future field added to either is not carried here unless
    somebody edits this function, which is the review the content rule needs.

    Args:
        message: The classified message. Only its id is read.
        decision: What ``classify`` returned. Only the folder and the
            deterministic flag are read; the reason string is NOT, because a
            classifier's prose can quote the subject it was reasoning about.

    Returns:
        The content-free record of what would have been filed where.
    """
    return PlannedFiling(
        message_id=message.id,
        folder=decision.folder,
        deterministic=decision.deterministic,
    )


def plan_only(
    message: Message,
    taxonomy: Taxonomy,
    classifier: Classifier | None,
    learned_rules: tuple[LearnedRule, ...] | None,
    plans: list[PlannedFiling],
) -> None:
    """Decide where this message WOULD go, record the plan, change nothing.

    The classification is the REAL one -- same ``classify`` call, same taxonomy,
    same learned rules, same coercion of a low-confidence answer to ``Review``
    -- because a preview that used a different decision procedure would be
    previewing something other than the pass it describes.

    A CLASSIFIER FAILURE IS NOT RECEIPTED HERE, and that is the one place this
    deliberately differs from the round's own apply path. That path writes a
    ``Status.FAILED`` row when ``classify`` raises, because a message was READ
    and no filing followed, which is a fact the owner is owed. In a dry round no
    filing was ever going to follow, so the ledger owes nothing -- and writing a
    row anyway would break the property the flag exists for, which is that a
    preview leaves no trace on disk. The exception therefore propagates, ending
    the round loudly rather than reporting a preview that silently omitted the
    messages it could not decide.

    Args:
        message: The in-scope message. Already past the scope and replay gates.
        taxonomy: The installed folder vocabulary.
        classifier: The injected decision-maker, or None.
        learned_rules: The owner's learned-rule table, or None.
        plans: The round's accumulator. Appended to, never read.
    """
    decision = classify(message, taxonomy, classifier, learned_rules=learned_rules)
    plans.append(plan(message, decision))


def announce_withheld_cursor(container_id: str | None, messages: int) -> None:
    """Say why a dry round is not moving the stream. Counts and a stream id only.

    The same content rule the round's own logging follows: a line explaining
    what was NOT filed must not quote what was not filed.

    Args:
        container_id: The stream this round polled. ``None`` is mailbox-wide.
        messages: How many messages the round saw, for the operator's sense of
            scale. Not which ones.
    """
    logger.debug(
        f"WATCH: cursor withheld, dry run container_id={container_id} "
        f"messages={messages} "
        "(nothing was filed, so the next real pass must be offered the same mail)"
    )
