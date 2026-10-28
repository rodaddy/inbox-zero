"""The Sprint 3 demo, executed as a test. Section 6 of the sprint plan, verbatim.

This module is the DEMO RECEIPT, not a unit test of anything. Every assertion
here restates a line the sprint plan promised the operator would see on their terminal,
and the whole file exists so that "it learns" is a transcript rather than a
claim. The unit-level proofs live elsewhere -- ``test_learned.py`` owns the
store, ``test_classify_learned_pass.py`` owns the wiring, ``test_watcher.py``
owns the round. What none of them assert is the SEQUENCE: that a wrong filing,
an owner correction, and a second message from the same sender compose into a
model call that stops happening.

The five steps, matching the sprint's section 6 one for one:

1. a round files a message from a vendor domain into the WRONG folder, and it
   costs a model call to get it wrong
2. the owner corrects it, and the correction becomes a rule
3. the rule table shows that rule
4. a SECOND message from the same sender is filed with ``deterministic=True``
   AND THE MODEL IS NOT CALLED -- the spy's call log is the proof, and it is
   the only step whose failure would mean the sprint did not land
5. the rule is disabled and a third message goes BACK to the model

WHY A SPY AND NOT A MOCK ASSERTED AFTER THE FACT. ``RecordingClassifier``
records every message it is shown, so step 4 can assert on the LIST rather than
on a call count that a later refactor could quietly satisfy. The list also
carries the ids, which is what lets step 4 say "the model saw m1 and m3 and
never m2" -- a sentence about which specific message stopped costing money,
which is the actual product claim.

NO NETWORK, NO CREDENTIAL, NO MODEL. The provider is a fake and the classifier
is injected, on the same terms as ``test_watcher.py``: ``poll_once`` takes both
as values precisely so the sequence can be driven with nothing live behind it.
The config and data dirs are redirected to ``tmp_path`` by the autouse
``isolated_dirs`` fixture in ``conftest.py``, so the real ledger, the real
learned-rules file, and the live Microsoft soak state are untouched.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest

from inbox_zero_connector.providers.base import (
    ChangeSet,
    Container,
    DraftResult,
    MailProvider,
    Message,
    SentMessage,
    SentSelection,
    Thread,
)
from inbox_zero_connector.services.triage import learned as learned
from inbox_zero_connector.services.triage.decide import Classification, classify
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState

INBOX_ID = "Label_INBOX"
VENDOR = "billing@somevendor.example"
"""The sender from the sprint text. A real-looking vendor address, and the
domain is a reserved ``.example`` name so no test can ever resolve it."""

WRONG_FOLDER = "Newsletters"
"""Where the model puts it, and where the owner does not want it."""

RIGHT_FOLDER = "Receipts"
"""Where the owner moves it. Both names are in the default taxonomy, so
``coerce`` passes them through untouched and the demo asserts the filing rather
than the coercion."""

NOW = datetime(2026, 8, 19, 17, 0, 0, tzinfo=UTC)
"""Supplied to ``record_correction`` rather than read from the clock, so the
``learned_at`` the transcript prints is the one this file chose."""


class DemoProvider(MailProvider):
    """A mailbox holding one scripted message at a time. Records every filing.

    Follows the ``FakeProvider`` pattern in ``test_watcher.py``: the unused half
    of the interface raises, so a step that accidentally reaches an
    unimplemented capability fails loudly instead of reading as a pass. The
    send-shaped members raise ``AssertionError`` specifically -- this connector
    has no send capability and the demo must not appear to exercise one.
    """

    name = "google"

    def __init__(self) -> None:
        self.changes = ChangeSet(messages=(), cursor="cursor-0")
        self.filed: list[tuple[str, str]] = []

    def deliver(self, message: Message, cursor: str) -> None:
        """Put one message in the mailbox for the next round to find."""
        self.changes = ChangeSet(messages=(message,), cursor=cursor)

    def authenticate(self, interactive: bool = False) -> str:
        raise NotImplementedError

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        raise NotImplementedError

    def get_thread(self, thread_id: str) -> Thread:
        raise NotImplementedError

    def list_containers(self) -> list[Container]:
        return [
            Container(
                id=INBOX_ID,
                name="Inbox",
                well_known="inbox",
                parent_id=None,
                exclusive=False,
            )
        ]

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        return self.changes

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        return folder

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        raise NotImplementedError

    def set_important(self, message_id: str, important: bool = True) -> str:
        raise AssertionError("the demo files messages; it never flags them")

    def tag_message(self, message_id: str, label: str) -> str:
        raise AssertionError("the demo files messages out of the inbox")

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        raise AssertionError("the demo never drafts and this connector never sends")

    def create_self_draft(self, subject: str, body: str) -> str:
        raise AssertionError("the demo never drafts and this connector never sends")


class RecordingClassifier:
    """The model layer, replaced by a spy that records what it was asked.

    ``seen_ids`` is the assertion surface for the entire demo. A message id in
    this list is a message that cost a model call; an id absent from it is a
    message the deterministic layer settled for free. Step 4 asserts the
    ABSENCE, which is the sprint's whole claim.

    It always answers ``Newsletters`` -- deliberately the wrong folder -- so the
    correction the owner makes in step 2 is a correction of something the model
    actually did, rather than a rule invented against a filing that never
    happened.
    """

    def __init__(self) -> None:
        self.seen_ids: list[str] = []

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        self.seen_ids.append(message.id)
        return Classification(
            folder=WRONG_FOLDER,
            confidence=0.9,
            reason="the model's guess: this looks like a bulk vendor mailing",
            deterministic=False,
        )


def say(line: str) -> None:
    """Emit one transcript line to stdout.

    ``sys.stdout.write`` rather than ``print``: T20 (flake8-print) is enabled
    package-wide and tests carry no ``T20`` exemption in ``pyproject.toml``,
    which is correct -- a stray ``print`` in a test is noise, and the MCP server
    shares this process's stdout with the protocol. The transcript lines here
    are not stray: they ARE the deliverable this module exists to produce, which
    is why they go through one named function that says so rather than through
    seven bare calls that read like debugging left behind.
    """
    sys.stdout.write(f"{line}\n")


def _vendor_message(message_id: str) -> Message:
    """A header projection from the vendor that no hand-written rule settles.

    Ordinary subject, real sender, not flagged, no Gmail category label -- so
    ``classify.RULES`` falls through and the message reaches whichever layer the
    step under demonstration is about. A message the rule table settled would
    never reach either the learned pass or the model, and the demo would prove
    nothing about either.
    """
    return Message(
        id=message_id,
        thread_id=f"t-{message_id}",
        sender=VENDOR,
        subject="Your July invoice is ready",
        received="2026-08-19T09:00:00Z",
        snippet="Invoice attached",
        containers=(INBOX_ID,),
    )


def _round(
    provider: DemoProvider,
    classifier: RecordingClassifier,
    state: WatchState,
    rules: tuple[learned.LearnedRule, ...],
) -> watcher.RoundSummary:
    """One real ``poll_once`` round with the table the owner has taught so far.

    The rules are passed EXPLICITLY rather than left to ``poll_once``'s own
    load, so each step states the table it is running against and the transcript
    shows the learning as an argument that changed between rounds.
    """
    return watcher.poll_once(
        watcher.RoundContext(
            provider=provider,
            scope=MailboxScope(mode="all_folders"),
            taxonomy=default_taxonomy(),
            state=state,
            container_id=None,
            classifier=classifier,
            learned_rules=rules,
        )
    )


def _step1_the_model_gets_it_wrong(
    provider: DemoProvider, classifier: RecordingClassifier, state: WatchState
) -> None:
    """A round files the vendor's first message into the WRONG folder.

    The model call is the point as much as the wrong answer is: this message
    cost one, and step 4's whole claim is that its successor does not.
    """
    provider.deliver(_vendor_message("m1"), "cursor-1")
    summary = _round(provider, classifier, state, ())

    assert provider.filed == [("m1", WRONG_FOLDER)]
    assert classifier.seen_ids == ["m1"], "step 1 must reach the model"
    assert summary.applied == 1
    say(f"STEP 1: filed m1 -> {WRONG_FOLDER} (wrong); model calls so far: {classifier.seen_ids}")


def _step2_the_owner_corrects_it() -> learned.LearnedRule:
    """The owner moves it to Receipts. The correction becomes a row on disk."""
    rows, outcome = learned.record_correction(
        (),
        sender=VENDOR,
        containers=(INBOX_ID,),
        folder=RIGHT_FOLDER,
        message_id="m1",
        now=NOW,
    )
    learned.save_rules(rows)

    assert outcome is learned.CorrectionOutcome.CREATED
    assert len(rows) == 1
    say(f"STEP 2: correction recorded, outcome={outcome.value}")
    return rows[0]


def _step3_the_rule_is_listed() -> tuple[learned.LearnedRule, ...]:
    """``rules --list`` shows it: predicate, value, folder, timestamp, enabled.

    Read back from the FILE rather than reused from step 2's return value, so
    this asserts what the owner's ``rules --list`` would actually print.

    NOTE the predicate is ``sender_is``, not the ``sender_domain_is`` the sprint
    text shows. One correction produces an address-exact row;
    ``learned.ESCALATION_THRESHOLD`` is 3, so the domain rule is earned by the
    third correction from that domain. See FINDING 2 in the transcript.
    """
    on_disk = learned.load_rules()

    assert len(on_disk) == 1
    rule = on_disk[0]
    assert rule.predicate is learned.Predicate.SENDER_IS
    assert rule.value == VENDOR
    assert rule.folder == RIGHT_FOLDER
    assert rule.enabled is True
    assert rule.source_message_id == "m1"
    say(
        f"STEP 3: rules --list -> id={rule.id} {rule.predicate.value} {rule.value} "
        f"-> {rule.folder} learned_at={rule.learned_at} enabled={rule.enabled}"
    )
    return on_disk


def _step4_the_second_message_is_free(
    provider: DemoProvider,
    classifier: RecordingClassifier,
    state: WatchState,
    on_disk: tuple[learned.LearnedRule, ...],
) -> None:
    """THE DEMO. A second message from the same sender, filed with NO model call.

    Two assertions carry the sprint, and they are different claims. That the
    message landed in ``Receipts`` says the rule fired. That
    ``classifier.seen_ids`` did not grow says the MODEL WAS NOT ASKED -- which
    is the one a filing that happened to be right could not produce.
    """
    calls_before = list(classifier.seen_ids)
    provider.deliver(_vendor_message("m2"), "cursor-2")
    _round(provider, classifier, state, on_disk)

    assert provider.filed[-1] == ("m2", RIGHT_FOLDER), "the learned rule filed it"
    assert classifier.seen_ids == calls_before, "THE MODEL WAS NOT CALLED FOR m2"
    assert "m2" not in classifier.seen_ids

    # ``classify`` is PURE, so re-deriving the decision the round just acted on
    # reads the receipt without re-running the round. The re-derivation must not
    # call the model either, which is asserted rather than assumed.
    decision = classify(
        _vendor_message("m2"), default_taxonomy(), classifier, learned_rules=on_disk
    )

    assert decision.deterministic is True
    assert decision.folder == RIGHT_FOLDER
    assert decision.confidence == pytest.approx(1.0)
    assert classifier.seen_ids == calls_before, "re-deriving must not call the model either"
    say(
        f"STEP 4: filed m2 -> {decision.folder} deterministic={decision.deterministic} "
        f"confidence={decision.confidence} reason={decision.reason!r}"
    )
    say(f"STEP 4: model calls unchanged: {classifier.seen_ids} -- m2 never reached the model")


def _step5_disabling_gives_it_back(
    provider: DemoProvider,
    classifier: RecordingClassifier,
    state: WatchState,
    rule: learned.LearnedRule,
) -> None:
    """The owner disables the rule and the third message goes BACK to the model.

    Learning is reversible, and the reversal is observable at the same surface
    that proved the learning: the spy's call log grows again.
    """
    calls_before = list(classifier.seen_ids)
    learned.save_rules(learned.disable_rule(learned.load_rules(), rule.id, "disabled in the demo"))
    reloaded = learned.load_rules()
    assert reloaded[0].enabled is False

    provider.deliver(_vendor_message("m3"), "cursor-3")
    _round(provider, classifier, state, reloaded)

    assert classifier.seen_ids == [*calls_before, "m3"], "m3 went BACK to the model"
    assert provider.filed[-1] == ("m3", WRONG_FOLDER), "the model's answer again"
    say(
        f"STEP 5: rule {rule.id} disabled; filed m3 -> {WRONG_FOLDER}; "
        f"model calls now: {classifier.seen_ids} -- learning is reversible"
    )


def test_the_sprint3_demo_end_to_end() -> None:
    """The five steps in one test, because the SEQUENCE is the claim.

    Split into five independent tests these would share a fixture holding the
    state the steps mutate, and the thing being demonstrated -- that step 4's
    behaviour is CAUSED by step 2 -- would live in that fixture rather than in
    anything a reader can see. So the steps are five named functions and this is
    the narrative that orders them: one provider, one classifier spy, and one
    watch state threaded through all five, exactly as one mailbox would be.
    """
    provider = DemoProvider()
    classifier = RecordingClassifier()
    state = WatchState()

    _step1_the_model_gets_it_wrong(provider, classifier, state)
    rule = _step2_the_owner_corrects_it()
    on_disk = _step3_the_rule_is_listed()
    _step4_the_second_message_is_free(provider, classifier, state, on_disk)
    _step5_disabling_gives_it_back(provider, classifier, state, rule)
