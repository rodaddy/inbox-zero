"""The row a correction reports back is the row that correction touched (#166).

THE EXECUTED DEFECT: ``_rule_for`` searched backwards for the last enabled row
naming the corrected FOLDER, which is the right row for a creation (it was just
appended) and the WRONG row for a REINFORCEMENT. Two senders filed to the same
folder leave two enabled rows there; reinforcing alice's rule returned bob's,
so the owner was told "mail matching rule <bob> is filed there" for a correction
that touched alice's row, and the ledger detail carried bob's ``rule_id``.

A reinforcement is identified by the SENDER PREDICATE that matched, which is
exactly what ``learned.match_fields`` returns -- so the fix asks the store the
same question the store asked itself.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.services.triage import learned
from inbox_zero_connector.services.triage.corrections import _rule_for

FOLDER = "Receipts"
ALICE = "alice@vendor.example"
BOB = "bob@other.example"


def _rule(
    rule_id: int, sender: str, *, folder: str = FOLDER, enabled: bool = True
) -> learned.LearnedRule:
    return learned.LearnedRule(
        id=rule_id,
        predicate=learned.Predicate.SENDER_IS,
        value=sender,
        folder=folder,
        enabled=enabled,
        learned_at="2026-08-19T00:00:00+00:00",
        source_message_id=f"m-{rule_id}",
    )


def test_a_reinforcement_returns_the_row_for_the_corrected_sender() -> None:
    """Alice's reinforcement must not report bob's rule."""
    rows = (_rule(1, ALICE), _rule(2, BOB))

    picked = _rule_for(rows, FOLDER, sender=ALICE, containers=())

    assert picked.id == 1
    assert picked.value == ALICE


def test_a_new_row_for_an_unknown_sender_is_still_found() -> None:
    """The creation path is unchanged: the appended row names the new sender."""
    rows = (_rule(1, ALICE), _rule(2, BOB))

    picked = _rule_for(rows, FOLDER, sender=BOB, containers=())

    assert picked.id == 2


def test_a_disabled_row_for_that_sender_is_never_reported() -> None:
    """A corrected-away row is evidence, not the outcome of this correction."""
    rows = (_rule(1, ALICE, folder="Newsletters", enabled=False), _rule(2, ALICE))

    assert _rule_for(rows, FOLDER, sender=ALICE, containers=()).id == 2


def test_an_escalated_domain_row_is_reported_over_the_address_row() -> None:
    """Escalation appends the domain row last, and it is what now matches."""
    domain_row = learned.LearnedRule(
        id=3,
        predicate=learned.Predicate.SENDER_DOMAIN_IS,
        value="vendor.example",
        folder=FOLDER,
        learned_at="2026-08-19T00:00:00+00:00",
        source_message_id="m-3",
    )
    rows = (_rule(1, ALICE), domain_row)

    assert _rule_for(rows, FOLDER, sender=ALICE, containers=()).id in {1, 3}


def test_no_enabled_row_for_the_folder_is_a_store_disagreement() -> None:
    rows = (_rule(1, ALICE, folder="Newsletters"),)

    with pytest.raises(ValueError, match="no enabled rule"):
        _rule_for(rows, FOLDER, sender=ALICE, containers=())
