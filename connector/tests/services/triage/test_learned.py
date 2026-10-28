"""Tests for the learned-rule store.

No mail, no model, no network, no credential. The store is a JSON file and a
handful of pure functions over rows, and that is exactly why it can be proven
here: ``record_correction`` is the only writer, it takes ``now`` as an
argument, and every predicate reads a field the PROVIDER stated rather than
anything a sender wrote.

The ``isolated_dirs`` autouse fixture in ``conftest.py`` redirects
``INBOX_ZERO_CONFIG_DIR`` at a ``tmp_path``, so ``learned_rules_path()``
resolves inside the test's own sandbox and no test can touch a real
deployment's learned rules.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_zero_connector.providers.base import Message
from inbox_zero_connector.services.triage.decide import REVIEW_FOLDER
from inbox_zero_connector.services.triage.learned import (
    FREE_MAIL_DOMAINS,
    CorrectionOutcome,
    LearnedRule,
    Predicate,
    disable_rule,
    learned_rules_path,
    load_rules,
    match,
    record_correction,
    save_rules,
)

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)


def _message(
    *,
    sender: str = "ann@vendor.example",
    containers: tuple[str, ...] = (),
    identifier: str = "m1",
) -> Message:
    """A header-only message. Content fields stay at their defaults on purpose.

    Every test in this file that passes a message leaves the content fields
    alone, because no predicate in this module may read them. A test that had
    to set one would be evidence the vocabulary had grown a content predicate.
    """
    return Message(
        id=identifier,
        thread_id=f"t-{identifier}",
        sender=sender,
        subject="",
        received="2026-08-19T12:00:00Z",
        containers=containers,
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


# -- the store ---------------------------------------------------------------


def test_the_path_sits_in_the_config_dir() -> None:
    assert learned_rules_path().name == "learned-rules.json"
    assert learned_rules_path().parent.is_dir()


def test_a_missing_file_loads_as_an_empty_table() -> None:
    assert load_rules() == ()


def test_save_then_reload_round_trips_every_field() -> None:
    rows = (_rule(identifier=1), _rule(identifier=2, folder="Receipts", enabled=False))
    save_rules(rows)
    assert load_rules() == rows


def test_an_unparseable_file_starts_fresh_instead_of_crashing() -> None:
    learned_rules_path().write_text("{ not json", encoding="utf-8")
    assert load_rules() == ()


def test_a_non_object_file_starts_fresh() -> None:
    learned_rules_path().write_text("[1, 2, 3]", encoding="utf-8")
    assert load_rules() == ()


def test_one_corrupt_row_is_skipped_and_the_others_survive() -> None:
    good = _rule(identifier=1)
    payload = {
        "version": 1,
        "rules": [
            good.model_dump(mode="json"),
            {"id": "not-an-int", "predicate": "sender_is"},
            _rule(identifier=3, folder="Receipts").model_dump(mode="json"),
        ],
    }
    learned_rules_path().write_text(json.dumps(payload), encoding="utf-8")
    rows = load_rules()
    assert [row.id for row in rows] == [1, 3]


def test_the_path_argument_overrides_the_default(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    save_rules((_rule(),), path=target)
    assert target.exists()
    assert load_rules(path=target) == (_rule(),)
    assert load_rules() == ()


# -- validation --------------------------------------------------------------


def test_a_row_naming_a_folder_called_review_is_a_legal_row(tmp_path: Path) -> None:
    """The MODEL no longer knows which folder is the uncertainty one (issue #168).

    It cannot: a validator is handed a row, never a taxonomy, so a literal
    ``"Review"`` check refuses an ordinary folder on a renamed mailbox and
    accepts the real uncertainty folder on the same one. The refusal moved to
    the two seams that hold the taxonomy -- ``classify`` and
    ``record_correction`` -- and construction is plain again.
    """
    target = tmp_path / "rules.json"
    save_rules((_rule(folder=REVIEW_FOLDER),), path=target)

    assert load_rules(path=target)[0].folder == REVIEW_FOLDER


def test_a_value_is_lowercased_at_validation() -> None:
    assert _rule(value="ANN@Vendor.Example").value == "ann@vendor.example"


def test_a_rule_is_frozen_and_forbids_extra_fields() -> None:
    row = _rule()
    with pytest.raises(ValueError, match="frozen"):
        row.folder = "Elsewhere"  # type: ignore[misc]
    with pytest.raises(ValueError, match="extra"):
        LearnedRule(
            id=1,
            predicate=Predicate.SENDER_IS,
            value="a@b.example",
            folder="Vendors",
            learned_at=NOW.isoformat(),
            source_message_id="m1",
            surprise=True,
        )


# -- matching ----------------------------------------------------------------


def test_no_rows_means_no_match() -> None:
    assert match((), _message()) is None


def test_sender_is_matches_the_normalised_display_form() -> None:
    hit = match((_rule(),), _message(sender="Ann <ANN@Vendor.Example>"))
    assert hit is not None
    assert hit.folder == "Vendors"
    assert hit.rule_id == 1
    assert "sender_is" in hit.reason


def test_sender_domain_is_matches_on_the_extracted_domain() -> None:
    rows = (_rule(predicate=Predicate.SENDER_DOMAIN_IS, value="vendor.example"),)
    hit = match(rows, _message(sender="Someone Else <bob@VENDOR.example>"))
    assert hit is not None
    assert hit.folder == "Vendors"


def test_container_is_matches_membership() -> None:
    rows = (_rule(predicate=Predicate.CONTAINER_IS, value="label_promotions"),)
    assert match(rows, _message(containers=("Label_Promotions",))) is not None
    assert match(rows, _message(containers=("Label_Other",))) is None


def test_the_first_enabled_matching_row_wins() -> None:
    rows = (
        _rule(identifier=7, folder="Vendors"),
        _rule(identifier=8, folder="Receipts"),
    )
    hit = match(rows, _message())
    assert hit is not None
    assert (hit.rule_id, hit.folder) == (7, "Vendors")


def test_a_disabled_row_never_matches() -> None:
    rows = (
        _rule(identifier=7, folder="Vendors", enabled=False),
        _rule(identifier=8, folder="Receipts"),
    )
    hit = match(rows, _message())
    assert hit is not None
    assert (hit.rule_id, hit.folder) == (8, "Receipts")


def test_an_unparseable_sender_matches_nothing() -> None:
    assert match((_rule(value=""),), _message(sender="not-an-address")) is None


# -- record_correction -------------------------------------------------------


def test_a_correction_with_no_matching_rule_creates_an_address_exact_rule() -> None:
    rows, outcome = record_correction(
        (),
        sender="Ann <ANN@Vendor.Example>",
        containers=(),
        folder="Vendors",
        message_id="m9",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.CREATED
    assert len(rows) == 1
    assert rows[0].predicate is Predicate.SENDER_IS
    assert rows[0].value == "ann@vendor.example"
    assert rows[0].folder == "Vendors"
    assert rows[0].source_message_id == "m9"
    assert rows[0].learned_at == NOW.isoformat()
    assert rows[0].correction_count == 1


def test_a_correction_agreeing_with_an_enabled_rule_only_bumps_the_count() -> None:
    rows, outcome = record_correction(
        (_rule(),),
        sender="ann@vendor.example",
        containers=(),
        folder="Vendors",
        message_id="m9",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.REINFORCED
    assert len(rows) == 1
    assert rows[0].correction_count == 2
    assert rows[0].enabled is True


def test_a_correction_disagreeing_with_an_enabled_rule_disables_it_and_replaces_it() -> None:
    rows, outcome = record_correction(
        (_rule(identifier=4, folder="Vendors"),),
        sender="ann@vendor.example",
        containers=(),
        folder="Receipts",
        message_id="m9",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.CORRECTED
    old = next(row for row in rows if row.id == 4)
    assert old.enabled is False
    assert old.disabled_reason == "corrected by owner"
    assert old.folder == "Vendors"
    fresh = next(row for row in rows if row.id != 4)
    assert (fresh.folder, fresh.enabled) == ("Receipts", True)


def test_ids_are_monotonic_across_a_disable_then_replace() -> None:
    rows, _ = record_correction(
        (_rule(identifier=4, folder="Vendors"),),
        sender="ann@vendor.example",
        containers=(),
        folder="Receipts",
        message_id="m9",
        now=NOW,
    )
    assert max(row.id for row in rows) == 5


def test_the_third_sender_on_one_domain_escalates_to_a_domain_rule() -> None:
    rows: tuple[LearnedRule, ...] = ()
    for index, local in enumerate(("ann", "bob"), start=1):
        rows, outcome = record_correction(
            rows,
            sender=f"{local}@vendor.example",
            containers=(),
            folder="Vendors",
            message_id=f"m{index}",
            now=NOW,
        )
        assert outcome is CorrectionOutcome.CREATED
    assert all(row.predicate is Predicate.SENDER_IS for row in rows)

    rows, outcome = record_correction(
        rows,
        sender="cara@vendor.example",
        containers=(),
        folder="Vendors",
        message_id="m3",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.ESCALATED
    domain_rows = [row for row in rows if row.predicate is Predicate.SENDER_DOMAIN_IS]
    assert [(row.value, row.folder) for row in domain_rows] == [("vendor.example", "Vendors")]
    sender_rows = [row for row in rows if row.predicate is Predicate.SENDER_IS]
    assert len(sender_rows) == 3, "the address-exact rows stay"


def test_escalation_ignores_disabled_sender_rows() -> None:
    """A route the owner took back is not evidence for a domain-wide rule."""
    disabled_two = (
        _rule(identifier=1, value="ann@vendor.example", folder="Vendors", enabled=False),
        _rule(identifier=2, value="bob@vendor.example", folder="Vendors", enabled=False),
        _rule(identifier=3, value="dan@vendor.example", folder="Vendors"),
    )
    _, outcome = record_correction(
        disabled_two,
        sender="cara@vendor.example",
        containers=(),
        folder="Vendors",
        message_id="m4",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.CREATED

    all_enabled = tuple(row.model_copy(update={"enabled": True}) for row in disabled_two)
    _, outcome = record_correction(
        all_enabled,
        sender="cara@vendor.example",
        containers=(),
        folder="Vendors",
        message_id="m4",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.ESCALATED


def test_escalation_needs_three_rows_pointing_at_one_folder() -> None:
    rows = (
        _rule(identifier=1, value="ann@vendor.example", folder="Vendors"),
        _rule(identifier=2, value="bob@vendor.example", folder="Receipts"),
    )
    rows, outcome = record_correction(
        rows,
        sender="cara@vendor.example",
        containers=(),
        folder="Vendors",
        message_id="m3",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.CREATED
    assert not [row for row in rows if row.predicate is Predicate.SENDER_DOMAIN_IS]


@pytest.mark.parametrize("domain", sorted(FREE_MAIL_DOMAINS))
def test_a_free_mail_domain_never_escalates(domain: str) -> None:
    """gmail.com is three million senders, not one correspondent."""
    rows = (
        _rule(identifier=1, value=f"ann@{domain}", folder="Vendors"),
        _rule(identifier=2, value=f"bob@{domain}", folder="Vendors"),
    )
    rows, outcome = record_correction(
        rows,
        sender=f"cara@{domain}",
        containers=(),
        folder="Vendors",
        message_id="m3",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.CREATED
    assert not [row for row in rows if row.predicate is Predicate.SENDER_DOMAIN_IS]


def test_a_correction_targeting_the_uncertainty_folder_is_refused() -> None:
    """The caller names the uncertainty folder; this module never guesses it."""
    with pytest.raises(ValueError, match="stop guessing at this sender"):
        record_correction(
            (),
            sender="ann@vendor.example",
            containers=(),
            folder="Triage-Me",
            message_id="m9",
            now=NOW,
            review_folder="Triage-Me",
        )


def test_a_correction_targeting_an_ordinary_folder_named_review_is_recorded() -> None:
    """On a renamed taxonomy, ``Review`` is an ordinary destination."""
    rows, outcome = record_correction(
        (),
        sender="ann@vendor.example",
        containers=(),
        folder=REVIEW_FOLDER,
        message_id="m9",
        now=NOW,
        review_folder="Triage-Me",
    )

    assert outcome is CorrectionOutcome.CREATED
    assert rows[-1].folder == REVIEW_FOLDER


def test_a_correction_from_an_unparseable_sender_is_refused() -> None:
    with pytest.raises(ValueError, match="sender"):
        record_correction(
            (),
            sender="not-an-address",
            containers=(),
            folder="Vendors",
            message_id="m9",
            now=NOW,
        )


def test_a_container_rule_is_what_a_correction_agrees_with_when_it_matched() -> None:
    rows = (_rule(predicate=Predicate.CONTAINER_IS, value="label_promotions", folder="Vendors"),)
    rows, outcome = record_correction(
        rows,
        sender="ann@vendor.example",
        containers=("Label_Promotions",),
        folder="Vendors",
        message_id="m9",
        now=NOW,
    )
    assert outcome is CorrectionOutcome.REINFORCED
    assert rows[0].correction_count == 2


# -- disable_rule ------------------------------------------------------------


def test_disable_rule_disables_the_named_row_and_records_the_reason() -> None:
    rows = disable_rule((_rule(identifier=4), _rule(identifier=5)), 4, "owner asked")
    disabled = next(row for row in rows if row.id == 4)
    assert disabled.enabled is False
    assert disabled.disabled_reason == "owner asked"
    assert next(row for row in rows if row.id == 5).enabled is True


def test_disable_rule_on_an_unknown_id_names_the_known_ids() -> None:
    with pytest.raises(ValueError, match=r"4, 5"):
        disable_rule((_rule(identifier=4), _rule(identifier=5)), 99, "typo")


# -- the refusals, asserted rather than only documented ----------------------


def test_the_predicate_vocabulary_is_closed_at_three() -> None:
    assert {member.value for member in Predicate} == {
        "sender_is",
        "sender_domain_is",
        "container_is",
    }
