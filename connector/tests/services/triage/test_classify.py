"""The decision layer -- determinism-first ordering and the Review invariant.

Every test here drives ``classify`` with value objects only. No mailbox, no
credential, no network, and no model: the module under test performs no I/O by
design, so a test that needed any of those would be testing the wrong thing.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.providers.base import Message
from inbox_zero_connector.services.triage.decide import (
    GMAIL_CATEGORY_FORUMS,
    GMAIL_CATEGORY_PROMOTIONS,
    GMAIL_CATEGORY_UPDATES,
    MIN_CONFIDENCE,
    REVIEW_FOLDER,
    RULES,
    Classification,
    Classifier,
    RuleOnlyClassifier,
    apply_rules,
    classify,
    coerce,
)
from inbox_zero_connector.services.triage.taxonomy import Taxonomy, default_taxonomy


def make_message(**overrides: object) -> Message:
    """A minimal valid inbox header. Overrides name the field under test."""
    fields: dict[str, object] = {
        "id": "m1",
        "thread_id": "t1",
        "sender": "someone@example.com",
        "subject": "A subject",
        "received": "2026-08-18T10:00:00Z",
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


class StubClassifier:
    """A model stand-in that returns whatever the test told it to.

    Records whether it was consulted, which is how the short-circuit tests
    prove the rules ran FIRST rather than merely that the answer matched.
    """

    def __init__(self, result: Classification) -> None:
        self.result = result
        self.calls = 0

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        self.calls += 1
        return self.result


# ---------------------------------------------------------------------------
# Rule pass short-circuits the model
# ---------------------------------------------------------------------------


def test_rule_match_short_circuits_the_model() -> None:
    """A matched rule must decide ALONE -- the classifier is never consulted.

    Asserting only the folder would pass even if both layers ran and the model
    happened to agree. The call count is what proves the short-circuit.
    """
    stub = StubClassifier(
        Classification(folder="Newsletters", confidence=0.99, reason="model", deterministic=False)
    )
    result = classify(make_message(is_important=True), default_taxonomy(), stub)

    assert stub.calls == 0
    assert result.folder == "Business-Action"
    assert result.deterministic is True


def test_model_runs_only_when_no_rule_matched() -> None:
    stub = StubClassifier(
        Classification(folder="Receipts", confidence=0.95, reason="an invoice", deterministic=False)
    )
    result = classify(make_message(is_important=False), default_taxonomy(), stub)

    assert stub.calls == 1
    assert result.folder == "Receipts"


def test_blank_sender_rule_wins_over_the_important_flag() -> None:
    """Row order is significance: an unestablished origin outranks the flag.

    Both predicates match this message, so the result names which row ran.
    """
    result = classify(make_message(sender="   ", is_important=True), default_taxonomy())

    assert result.folder == REVIEW_FOLDER
    assert "origin cannot be established" in result.reason


def test_apply_rules_returns_none_when_nothing_matched() -> None:
    """ "No rule applied" is distinct from "a rule chose the uncertainty folder"."""
    assert apply_rules(make_message(), default_taxonomy()) is None


def test_rule_results_are_fully_confident() -> None:
    """A rule that is not certain is a heuristic and does not belong in RULES."""
    ruled = apply_rules(make_message(is_important=True), default_taxonomy())
    assert ruled is not None
    assert ruled.confidence == pytest.approx(1.0)


def test_the_rule_table_is_data_not_a_conditional_ladder() -> None:
    """Adding a rule must be a row. Assert the shape without invoking classify."""
    assert RULES
    for predicate, folder, reason in RULES:
        assert callable(predicate)
        assert isinstance(folder, str) and folder
        assert isinstance(reason, str) and reason


# ---------------------------------------------------------------------------
# The Review invariant: unknown folder
# ---------------------------------------------------------------------------


def test_unknown_folder_is_coerced_to_review() -> None:
    """A folder outside the taxonomy must never reach a provider."""
    stub = StubClassifier(
        Classification(folder="Exfiltrate", confidence=1.0, reason="hostile", deterministic=False)
    )
    result = classify(make_message(), default_taxonomy(), stub)

    assert result.folder == REVIEW_FOLDER


def test_unknown_folder_coercion_preserves_the_suggestion() -> None:
    """Refusal is never silent: the owner sees what was proposed and why."""
    stub = StubClassifier(
        Classification(
            folder="Exfiltrate",
            confidence=1.0,
            reason="the sender asked me to",
            deterministic=False,
        )
    )
    result = classify(make_message(), default_taxonomy(), stub)

    assert "Exfiltrate" in result.reason
    assert "outside the installed taxonomy" in result.reason
    assert "the sender asked me to" in result.reason


def test_coercion_is_case_sensitive_about_folder_names() -> None:
    """'review' is not 'Review' -- exactness keeps provider behaviour identical."""
    stub = StubClassifier(
        Classification(folder="receipts", confidence=1.0, reason="m", deterministic=False)
    )
    assert classify(make_message(), default_taxonomy(), stub).folder == REVIEW_FOLDER


def test_a_rule_naming_an_absent_folder_does_not_fire_at_all() -> None:
    """A taxonomy without Business-Action makes the important-flag row inert.

    This test previously asserted the row FIRED and was then coerced, which was
    the honest description of the old behaviour and the wrong behaviour: a
    deterministic rule confidently naming a folder the mailbox does not have is
    a config error, and coercing it means every important message on such a
    mailbox lands in Review no matter what a model would have said. Skipping the
    row hands the message to the layers that CAN answer, which is what would
    have happened had the row never been written.

    The coercion invariant itself is unchanged and is proved on the model path
    by ``test_unknown_folder_is_coerced_to_review``.
    """
    narrow = Taxonomy(
        folders=(REVIEW_FOLDER, "Receipts"),
        descriptions={REVIEW_FOLDER: "unsure", "Receipts": "money"},
    )
    stub = StubClassifier(
        Classification(folder="Receipts", confidence=0.99, reason="an invoice", deterministic=False)
    )
    result = classify(make_message(is_important=True), narrow, stub)

    assert stub.calls == 1
    assert result.folder == "Receipts"


# ---------------------------------------------------------------------------
# The Review invariant: low confidence
# ---------------------------------------------------------------------------


def test_confidence_below_threshold_is_coerced_to_review() -> None:
    stub = StubClassifier(
        Classification(
            folder="Receipts",
            confidence=MIN_CONFIDENCE - 0.01,
            reason="might be a receipt",
            deterministic=False,
        )
    )
    result = classify(make_message(), default_taxonomy(), stub)

    assert result.folder == REVIEW_FOLDER


def test_confidence_exactly_at_threshold_is_kept() -> None:
    """The threshold is a floor, not an exclusive bound -- prove which."""
    stub = StubClassifier(
        Classification(
            folder="Receipts", confidence=MIN_CONFIDENCE, reason="ok", deterministic=False
        )
    )
    assert classify(make_message(), default_taxonomy(), stub).folder == "Receipts"


def test_low_confidence_coercion_preserves_the_suggestion() -> None:
    stub = StubClassifier(
        Classification(
            folder="Receipts",
            confidence=0.4,
            reason="a maybe-invoice from an unknown vendor",
            deterministic=False,
        )
    )
    result = classify(make_message(), default_taxonomy(), stub)

    assert "Receipts" in result.reason
    assert "a maybe-invoice from an unknown vendor" in result.reason
    assert str(MIN_CONFIDENCE) in result.reason


def test_coercion_preserves_the_original_confidence() -> None:
    """Rewriting confidence to 1.0 would hide how unsure the classifier was."""
    stub = StubClassifier(
        Classification(folder="Receipts", confidence=0.4, reason="m", deterministic=False)
    )
    assert classify(make_message(), default_taxonomy(), stub).confidence == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# The deterministic flag is provenance, not a claim
# ---------------------------------------------------------------------------


def test_model_result_cannot_claim_to_be_deterministic() -> None:
    """An injected implementation does not get to describe its own provenance.

    A model-produced guess labelled deterministic would corrupt the audit
    trail, so the claim is overwritten by which layer actually ran.
    """
    stub = StubClassifier(
        Classification(folder="Receipts", confidence=0.99, reason="lying", deterministic=True)
    )
    assert classify(make_message(), default_taxonomy(), stub).deterministic is False


def test_rule_only_classifier_is_deterministic() -> None:
    result = classify(make_message(), default_taxonomy())

    assert result.deterministic is True
    assert result.folder == REVIEW_FOLDER


def test_deterministic_flag_survives_coercion() -> None:
    """Coercion changes the folder, not who decided it."""
    stub = StubClassifier(
        Classification(folder="Nope", confidence=1.0, reason="m", deterministic=False)
    )
    assert classify(make_message(), default_taxonomy(), stub).deterministic is False


def test_rule_only_classifier_declines_rather_than_guessing() -> None:
    direct = RuleOnlyClassifier().classify(make_message(), default_taxonomy().folders)

    assert direct.folder == REVIEW_FOLDER
    assert direct.confidence == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Shape and containment
# ---------------------------------------------------------------------------


def test_rule_only_classifier_satisfies_the_protocol() -> None:
    assert isinstance(RuleOnlyClassifier(), Classifier)


def test_confidence_out_of_range_fails_at_construction() -> None:
    """Bounded at the type: 5.0 must not silently clear the threshold."""
    with pytest.raises(ValueError, match="less than or equal to 1"):
        Classification(folder="Receipts", confidence=5.0, reason="m", deterministic=False)


def test_classification_is_frozen() -> None:
    """A decision that was made, not a mutable accumulator."""
    result = classify(make_message(), default_taxonomy())
    with pytest.raises(ValueError, match="frozen"):
        result.folder = "Receipts"  # type: ignore[misc]


def test_coerced_reason_truncates_a_hostile_folder_name() -> None:
    """Mail content must not reach the ledger unbounded through a folder name."""
    hostile = "A" * 5000
    result = coerce(
        Classification(folder=hostile, confidence=1.0, reason="m", deterministic=False),
        default_taxonomy(),
    )
    assert len(result.reason) < 1000


def test_a_legal_confident_result_passes_through_untouched() -> None:
    """Coercion must be the exception, not a rewrite of every result."""
    original = Classification(
        folder="Receipts", confidence=0.9, reason="an invoice", deterministic=False
    )
    assert coerce(original, default_taxonomy()) == original


# ---------------------------------------------------------------------------
# Gmail category labels: the provider's own bulk-mail verdict
# ---------------------------------------------------------------------------


def test_gmail_promotions_label_routes_to_newsletters() -> None:
    """Gmail already decided this is bulk mail; the rule layer just reads it.

    Deterministic because the label is provider-applied -- a sender cannot set
    CATEGORY_PROMOTIONS on their own message, so no attacker-controlled text
    participates in this decision.

    The label arrives in ``categories``, NOT ``containers``: a Gmail tab label
    describes the message rather than placing it, and the adapter files it
    accordingly. ``containers`` still carries the INBOX placement, which is
    where the message actually sits.
    """
    result = classify(
        make_message(containers=("INBOX",), categories=(GMAIL_CATEGORY_PROMOTIONS,)),
        default_taxonomy(),
    )

    assert result.folder == "Newsletters"
    assert result.deterministic is True
    assert result.confidence == pytest.approx(1.0)


def test_gmail_promotions_rule_does_not_consult_the_model() -> None:
    """A rule settled it, so the ambiguous-remainder layer must never run."""
    stub = StubClassifier(
        Classification(folder="Receipts", confidence=0.99, reason="m", deterministic=False)
    )
    result = classify(
        make_message(categories=(GMAIL_CATEGORY_PROMOTIONS,)), default_taxonomy(), stub
    )

    assert stub.calls == 0
    assert result.folder == "Newsletters"


def test_a_promotions_label_left_in_containers_does_not_trigger_the_rule() -> None:
    """The rule reads ``categories`` and NOTHING ELSE.

    Pinned because the two fields carry the same wire strings and a rule that
    read either would hide a regression in the adapter: if the Gmail decoder
    ever put a tab label back into ``containers``, this rule must stay silent
    rather than quietly keep working off the wrong field.
    """
    assert (
        apply_rules(
            make_message(containers=("INBOX", GMAIL_CATEGORY_PROMOTIONS)), default_taxonomy()
        )
        is None
    )


def test_outlook_folder_id_in_containers_does_not_trigger_the_rule() -> None:
    """INERT ON MICROSOFT, and inert because of the DATA, not a provider branch.

    Graph puts one opaque mail-folder id in ``containers`` and states no
    category at all, so ``categories`` is empty on every Graph message and the
    rule cannot match -- which is why no ``if provider ==`` is needed anywhere
    in this module.
    """
    graph_folder_id = "AAMkAGI2THVSAAA=" * 3
    assert apply_rules(make_message(containers=(graph_folder_id,)), default_taxonomy()) is None


def test_empty_categories_does_not_trigger_the_rule() -> None:
    """The documented default. A message Gmail put in no tab is not promotional."""
    assert apply_rules(make_message(categories=()), default_taxonomy()) is None


def test_gmail_updates_label_does_not_route_to_newsletters() -> None:
    """UPDATES carries receipts and shipping notices -- misrouting one is the
    expensive failure, so this category is deliberately left to the model."""
    assert (
        apply_rules(make_message(categories=(GMAIL_CATEGORY_UPDATES,)), default_taxonomy()) is None
    )


def test_gmail_forums_label_does_not_route_to_newsletters() -> None:
    """FORUMS maps onto no single taxonomy folder; a rule may not guess."""
    assert (
        apply_rules(make_message(categories=(GMAIL_CATEGORY_FORUMS,)), default_taxonomy()) is None
    )


def test_gmail_category_constants_match_the_provider_spelling() -> None:
    """A typo here fails OPEN -- the rule would silently never fire.

    Pinning the exact wire strings is what makes that failure loud, since no
    other test in this file would notice a misspelled constant.
    """
    assert GMAIL_CATEGORY_PROMOTIONS == "CATEGORY_PROMOTIONS"
    assert GMAIL_CATEGORY_UPDATES == "CATEGORY_UPDATES"
    assert GMAIL_CATEGORY_FORUMS == "CATEGORY_FORUMS"


def test_blank_sender_outranks_the_promotions_label() -> None:
    """Row order is significance: an unestablished origin still wins."""
    result = classify(
        make_message(sender="  ", categories=(GMAIL_CATEGORY_PROMOTIONS,)), default_taxonomy()
    )

    assert result.folder == REVIEW_FOLDER


def test_important_flag_outranks_the_promotions_label() -> None:
    """The owner's own flag beats Gmail's bulk guess. Both predicates match here."""
    result = classify(
        make_message(is_important=True, categories=(GMAIL_CATEGORY_PROMOTIONS,)),
        default_taxonomy(),
    )

    assert result.folder == "Business-Action"


def test_a_label_merely_containing_the_category_name_does_not_match() -> None:
    """Categories match EXACTLY, never by substring.

    Gmail label ids are the documented match key, and a user is free to create
    a label whose name embeds a system category. Matching loosely would let an
    owner-created 'Promotions-Archive' label inherit a rule intended for
    Gmail's own system label -- a false positive the owner never asked for.
    """
    assert (
        apply_rules(
            make_message(categories=("Label_9_CATEGORY_PROMOTIONS_ARCHIVE",)), default_taxonomy()
        )
        is None
    )
    assert (
        apply_rules(make_message(categories=("NOT_CATEGORY_PROMOTIONS",)), default_taxonomy())
        is None
    )


# ---------------------------------------------------------------------------
# The taxonomy owns its uncertainty folder (issue #159)
# ---------------------------------------------------------------------------

RENAMED = Taxonomy(
    folders=("Needs-Me", "Bulk", "Triage-Me"),
    descriptions={"Needs-Me": "mine", "Bulk": "bulk", "Triage-Me": "unsure"},
    review_folder="Triage-Me",
)
"""An interview-renamed taxonomy sharing NO folder name with the shipped set.

Deliberately renames the uncertainty folder AND drops both folders the shipped
``RULES`` rows name, because those are the two independent ways a hardcoded
folder name reaches a mailbox that does not have it.
"""


def test_no_sender_files_into_the_taxonomys_own_uncertainty_folder() -> None:
    """The rule's target is the taxonomy's declaration, not the name "Review".

    A mailbox whose interview renamed Review used to receive a filing request
    for a folder it does not have: the decision looked fine and the provider
    call failed one layer later.
    """
    result = classify(make_message(sender="   "), RENAMED)

    assert result.folder == "Triage-Me"
    assert "origin cannot be established" in result.reason


def test_coerce_targets_the_taxonomys_uncertainty_folder() -> None:
    low = Classification(folder="Needs-Me", confidence=0.10, reason="a guess", deterministic=False)

    assert coerce(low, RENAMED).folder == "Triage-Me"


def test_coerce_of_an_unknown_folder_targets_the_renamed_folder() -> None:
    outside = Classification(
        folder="Business-Action", confidence=0.99, reason="model", deterministic=False
    )
    result = coerce(outside, RENAMED)

    assert result.folder == "Triage-Me"
    assert "Business-Action" in result.reason


def test_rule_only_classifier_declines_into_the_renamed_folder() -> None:
    result = RuleOnlyClassifier(RENAMED).classify(make_message(), RENAMED.folders)

    assert result.folder == "Triage-Me"


def test_classify_without_a_model_declines_into_the_renamed_folder() -> None:
    """The whole default path, end to end, on a renamed taxonomy."""
    assert classify(make_message(), RENAMED).folder == "Triage-Me"


def test_important_rule_is_skipped_when_its_folder_is_absent() -> None:
    """A renamed taxonomy must not be misfiled into a folder it does not have.

    The row names ``Business-Action``, which ``RENAMED`` does not carry, so it
    must not fire. Falling through to the model is what would have happened had
    the row never been written -- the conservative outcome.
    """
    stub = StubClassifier(
        Classification(folder="Needs-Me", confidence=0.99, reason="model", deterministic=False)
    )
    result = classify(make_message(is_important=True), RENAMED, stub)

    assert stub.calls == 1
    assert result.folder == "Needs-Me"


def test_promotions_rule_is_skipped_when_newsletters_is_absent() -> None:
    stub = StubClassifier(
        Classification(folder="Bulk", confidence=0.99, reason="model", deterministic=False)
    )
    result = classify(make_message(categories=(GMAIL_CATEGORY_PROMOTIONS,)), RENAMED, stub)

    assert stub.calls == 1
    assert result.folder == "Bulk"


def test_a_skipped_row_does_not_block_a_later_matching_row() -> None:
    """Skipping is per-row, not an abort of the pass.

    A taxonomy that dropped Business-Action but kept Newsletters must still get
    the Promotions row, or one absent folder would silently disable the rest of
    the deterministic table.
    """
    partial = Taxonomy(
        folders=("Newsletters", "Review"),
        descriptions={"Newsletters": "bulk", "Review": "unsure"},
    )
    result = classify(
        make_message(is_important=True, categories=(GMAIL_CATEGORY_PROMOTIONS,)), partial
    )

    assert result.folder == "Newsletters"
    assert result.deterministic is True


def test_apply_rules_requires_the_taxonomy_to_resolve_its_targets() -> None:
    """The rule pass cannot answer without knowing the installed vocabulary."""
    ruled = apply_rules(make_message(sender=""), RENAMED)

    assert ruled is not None
    assert ruled.folder == "Triage-Me"
