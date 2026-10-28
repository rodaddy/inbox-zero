"""The learned pass INSIDE ``classify()`` -- the seam between the two layers.

``tests/test_learned.py`` proves the store: rows, matching, escalation, the
file. This file proves the WIRING, which is a different claim and fails in
different ways. The store can be perfect while the decision layer consults it
in the wrong order, coerces the wrong thing, or reaches a model for a message a
row already settled.

Three properties are asserted here and nowhere else:

* the learned pass costs NOTHING at the model layer. The injected classifier in
  these tests raises on call, so a test that reaches it fails loudly rather
  than passing slowly. That is what makes "no model call for a learned message"
  a proven fact rather than a design intention.
* hand-written ``RULES`` still win. They encode invariants -- no sender means
  ``Review``, always -- and a learned row is data the owner typed with their
  hands, so a table that could override the no-sender rule would let a filing
  habit outrank a safety property.
* a learned row still goes through ``coerce``. A learned rule is MORE likely to
  name a stale folder than a hand-written one, because the taxonomy can be
  re-interviewed long after the row was learned.

No file I/O anywhere in this module: ``classify`` takes the loaded table as an
argument, so these tests build tuples of rows directly and never touch a path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inbox_zero_connector.providers.base import Message
from inbox_zero_connector.services.triage.decide import (
    MIN_CONFIDENCE,
    REVIEW_FOLDER,
    Classification,
    classify,
)
from inbox_zero_connector.services.triage.learned import LearnedRule, Predicate
from inbox_zero_connector.services.triage.taxonomy import Taxonomy

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)

TAXONOMY = Taxonomy(
    folders=("Business-Action", "Newsletters", "Vendors", REVIEW_FOLDER),
    descriptions={},
)


class ExplodingClassifier:
    """A model layer that must never be reached. Reaching it is the failure.

    A ``Mock`` asserted on after the fact would report the same fact one step
    later and only if the assertion was remembered; raising at the call site
    names the exact test that let a settled message reach the model.
    """

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        raise AssertionError(
            "the model layer was consulted for a message a learned rule already settled"
        )


class RecordingClassifier:
    """Reached on purpose, for the cases where the model layer SHOULD run."""

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        self.calls += 1
        return Classification(
            folder="Newsletters",
            confidence=0.9,
            reason="model reached",
            deterministic=False,
        )


def _message(
    *,
    sender: str = "ann@vendor.example",
    containers: tuple[str, ...] = (),
    is_important: bool = False,
) -> Message:
    return Message(
        id="m1",
        thread_id="t-m1",
        sender=sender,
        subject="",
        received="2026-08-19T12:00:00Z",
        containers=containers,
        is_important=is_important,
    )


def _rule(
    *,
    identifier: int = 1,
    predicate: Predicate = Predicate.SENDER_IS,
    value: str = "ann@vendor.example",
    folder: str = "Vendors",
    enabled: bool = True,
) -> LearnedRule:
    return LearnedRule(
        id=identifier,
        predicate=predicate,
        value=value,
        folder=folder,
        enabled=enabled,
        learned_at=NOW.isoformat(),
        source_message_id="m1",
    )


def test_learned_rule_fires_deterministically_without_touching_the_model() -> None:
    """A matching row settles the message: folder, confidence 1.0, no model call."""
    result = classify(
        _message(),
        TAXONOMY,
        ExplodingClassifier(),
        learned_rules=(_rule(),),
    )

    assert result.folder == "Vendors"
    assert result.confidence == pytest.approx(1.0)
    assert result.deterministic is True
    assert "Learned rule 1" in result.reason


def test_hand_written_rules_beat_learned_rules() -> None:
    """Both layers match; RULES wins, and the reason proves WHICH one decided.

    ``is_important`` is a provider-stated fact and the hand-written row on it
    routes to Business-Action. The learned row names Vendors for the same
    sender. If the learned pass ran first the message would file to Vendors,
    and the owner's audit trail would say a learned habit outranked the
    provider's own signal.
    """
    result = classify(
        _message(is_important=True),
        TAXONOMY,
        ExplodingClassifier(),
        learned_rules=(_rule(),),
    )

    assert result.folder == "Business-Action"
    assert "Learned rule" not in result.reason
    assert result.deterministic is True


def test_learned_rule_naming_an_unknown_folder_coerces_to_review() -> None:
    """coerce runs on the learned layer too, and keeps the suggestion visible."""
    result = classify(
        _message(),
        TAXONOMY,
        ExplodingClassifier(),
        learned_rules=(_rule(folder="Archive-2019"),),
    )

    assert result.folder == REVIEW_FOLDER
    assert "Archive-2019" in result.reason
    assert "outside the installed taxonomy" in result.reason


@pytest.mark.parametrize("table", [None, ()])
def test_absent_or_empty_table_reaches_the_model_unchanged(
    table: tuple[LearnedRule, ...] | None,
) -> None:
    """No table and an empty table both behave exactly as before this pass existed."""
    decider = RecordingClassifier()

    result = classify(_message(), TAXONOMY, decider, learned_rules=table)

    assert decider.calls == 1
    assert result.folder == "Newsletters"
    assert result.deterministic is False
    assert result.confidence >= MIN_CONFIDENCE


def test_disabled_rule_does_not_fire() -> None:
    """A disabled row is evidence, not a decision. The model still runs."""
    decider = RecordingClassifier()

    result = classify(
        _message(),
        TAXONOMY,
        decider,
        learned_rules=(_rule(enabled=False),),
    )

    assert decider.calls == 1
    assert result.folder == "Newsletters"
    assert result.deterministic is False


# ---------------------------------------------------------------------------
# The taxonomy owns its uncertainty folder, so the store cannot (issue #168)
# ---------------------------------------------------------------------------

RENAMED = Taxonomy(
    folders=("Business-Action", "Newsletters", "Review", "Triage-Me"),
    descriptions={},
    review_folder="Triage-Me",
)
"""A taxonomy whose uncertainty folder is renamed AND which still has a Review.

Both halves matter. ``Triage-Me`` is the folder a learned row may never target,
and ``Review`` is an ordinary destination this mailbox happens to spell that
way -- the literal check this replaced got both of them backwards.
"""


def test_a_learned_rule_targeting_the_renamed_uncertainty_folder_never_wins() -> None:
    """Uncertainty is a state, not a destination a row may launder into fact."""
    decider = RecordingClassifier()

    result = classify(
        _message(),
        RENAMED,
        decider,
        learned_rules=(_rule(folder="Triage-Me"),),
    )

    assert decider.calls == 1
    assert result.folder == "Newsletters"
    assert "Learned rule" not in result.reason


def test_a_learned_rule_targeting_an_ordinary_folder_named_review_wins() -> None:
    """``Review`` is only special when the taxonomy says it is."""
    result = classify(
        _message(),
        RENAMED,
        ExplodingClassifier(),
        learned_rules=(_rule(folder="Review"),),
    )

    assert result.folder == "Review"
    assert result.deterministic is True
    assert "Learned rule" in result.reason
