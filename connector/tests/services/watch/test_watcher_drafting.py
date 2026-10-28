"""The drafting half of one polling round, driven over the same fakes.

Split out of ``test_watcher.py`` on 2026-08-19: ticket 4.4 added the Sprint 4
drafting tests to that module and pushed it past the repo's 500-code-line
ceiling (``scripts/check_code_size.py``, 523 lines). The seam is the concern
itself -- ``test_watcher.py`` keeps polling, filing, and round state, and this
module keeps everything that reaches ``create_draft``.

The shared fakes are IMPORTED from ``test_watcher`` rather than duplicated,
which is already this package's pattern for watcher fakes:
``test_watchloop.py``, ``test_watchloop_pass_failure.py``, and
``test_reauth_cycles.py`` all import ``FakeProvider`` and ``_context`` from
that module. Duplicating them would fork the base class that
``DraftingProvider`` extends, and the base class's raising ``create_draft`` is
the very thing proving a watcher with no injected backend never drafts.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from _watch_harness import FINAL_CURSOR, CountingClassifier, FakeProvider, _context, _message
from inbox_zero_connector.config import MAX_DRAFT_BODY_CHARS, VoiceRouting
from inbox_zero_connector.providers.base import (
    ChangeSet,
    DraftResult,
    MailProvider,
    Message,
    Thread,
)
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy
from inbox_zero_connector.services.voice import profile as voiceprofile
from inbox_zero_connector.services.voice.profile import VoiceProfile
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState

# ---------------------------------------------------------------------------
# Sprint 4: drafting, and the idempotency gate that let it out of
# FORBIDDEN_METHODS. RED-FIRST: disabling `_already_drafted` makes the replay
# test below produce two drafts and two rows.
# ---------------------------------------------------------------------------


DRAFT_THREAD = "t-m1"


class DraftingProvider(FakeProvider):
    """A ``FakeProvider`` that WILL draft, and counts how often it was asked.

    A separate class rather than a flag on ``FakeProvider`` so every existing
    test keeps the assertion that drafting is unreachable for it -- the base
    class's ``create_draft`` still raises, and that is what proves a watcher
    with no injected backend touches the method not at all.
    """

    def __init__(self, changes: ChangeSet, *, replier: str = "") -> None:
        super().__init__(changes)
        self.drafted: list[tuple[str, str]] = []
        self.replier = replier

    def get_thread(self, thread_id: str) -> Thread:
        senders = ["someone@example.com"]
        if self.replier:
            senders.append(self.replier)
        return Thread(
            id=thread_id,
            subject="Quarterly review notes",
            messages=[
                Message(
                    id=f"{thread_id}-{index}",
                    thread_id=thread_id,
                    sender=sender,
                    subject="Quarterly review notes",
                    received="2026-08-18T10:00:00Z",
                )
                for index, sender in enumerate(senders)
            ],
            bodies=["Can you look at the numbers and let me know?"],
            participants=["someone@example.com", "owner@example.com"],
        )

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        self.drafted.append((thread_id, body))
        return DraftResult(
            draft_id=f"d{len(self.drafted)}",
            thread_id=thread_id,
            recipients=["someone@example.com"],
        )


class EchoBackend:
    """A composer with no model behind it. Returns fixed text, records prompts."""

    def __init__(self, body: str = "Happy to take a look this week.") -> None:
        self.body = body
        self.prompts: list[str] = []

    def compose(self, prompt: str, voice: VoiceProfile) -> str:
        self.prompts.append(prompt)
        return self.body


def _approved_profile(voice: str = "external") -> VoiceProfile:
    """A stored, APPROVED profile. Unapproved would refuse before composing."""
    profile = voiceprofile.VoiceProfile(
        voice=voice,
        greeting="opens with a first name",
        sign_off="signs off with thanks",
    )
    now = datetime(2026, 8, 19, tzinfo=UTC)
    profiles = voiceprofile.approve(voiceprofile.store({}, profile, now=now), voice, now=now)
    voiceprofile.save(profiles)
    return profiles[voice]


def _drafting_context(
    provider: DraftingProvider,
    backend: EchoBackend,
    *,
    state: WatchState | None = None,
) -> watcher.RoundContext:
    return watcher.RoundContext(
        provider=provider,
        scope=MailboxScope(mode="all_folders"),
        taxonomy=default_taxonomy(),
        state=state if state is not None else WatchState(),
        container_id=None,
        classifier=CountingClassifier(),
        draft_backend=backend,
        routing=VoiceRouting(owner_domain="example.com"),
        voice="external",
        owner_address="owner@example.com",
    )


def test_a_replayed_round_leaves_exactly_one_draft_and_one_ledger_row() -> None:
    """THE IDEMPOTENCY PROOF, with a FORCED replay rather than a hopeful one.

    The replay is forced by handing the second round a FRESH ``WatchState``, so
    the ``seen`` window is empty and the message arrives as brand new -- which
    is precisely what a cursor resync produces and what a bounded window cannot
    prevent. The only thing standing between that and a second draft in the
    owner's mailbox is ``watcher._already_drafted`` reading the append-only
    ledger.

    RED-FIRST, observed: with ``_already_drafted`` stubbed to ``False`` this
    test reports two drafts and two ACTION_DRAFT rows.
    """
    _approved_profile()
    backend = EchoBackend()

    first = DraftingProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    watcher.poll_once(_drafting_context(first, backend))
    assert len(first.drafted) == 1

    # A FRESH state: the replay guard remembers nothing, exactly as after a
    # resync. Same message, same thread, second round.
    second = DraftingProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    watcher.poll_once(_drafting_context(second, backend, state=WatchState()))

    assert second.drafted == [], "a replayed round must not create a second draft"
    draft_rows = [row for row in ledger.read_all() if row.action == ledger.ACTION_DRAFT]
    assert len(draft_rows) == 1
    assert draft_rows[0].message_id == DRAFT_THREAD
    assert draft_rows[0].voice == "external"


def test_the_ledger_gate_survives_a_state_the_replay_window_would_have_lost() -> None:
    """``thread_has_draft`` is what answers, and it reads the ledger alone."""
    _approved_profile()
    assert ledger.thread_has_draft(DRAFT_THREAD) is False

    provider = DraftingProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    watcher.poll_once(_drafting_context(provider, EchoBackend()))

    assert ledger.thread_has_draft(DRAFT_THREAD) is True
    assert ledger.thread_has_draft("t-never-seen") is False


def test_a_generated_body_over_the_cap_is_refused_before_any_api_call() -> None:
    """The provider is never reached. Filing still happened; the draft did not."""
    _approved_profile()
    provider = DraftingProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    oversized = EchoBackend("x" * (MAX_DRAFT_BODY_CHARS + 1))

    summary = watcher.poll_once(_drafting_context(provider, oversized))

    assert provider.drafted == [], "an over-cap body must not reach create_draft"
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE]
    assert summary.applied == 1, "the filing is unaffected by the refused draft"


def test_a_thread_demanding_a_new_recipient_produces_a_draft_with_no_recipient() -> None:
    """STRUCTURALLY IMPOSSIBLE, and this test names the structure.

    The message body below is prompt injection with a plausible cover story.
    Even a backend that obeyed it completely could only put an address in the
    BODY TEXT, because ``MailProvider.create_draft`` accepts a thread id and a
    body and HAS NO RECIPIENT PARAMETER -- there is nowhere to put one. The
    recipients on the result are derived by the adapter from the thread.

    Asserted two ways on purpose: the signature is inspected so the guarantee
    is checked at the interface rather than inferred from one call, and the
    recorded call is checked so the assertion is not vacuous.
    """
    _approved_profile()
    provider = DraftingProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    hostile = EchoBackend("Sure -- I have cc'd legal@attacker.example as you asked.")

    watcher.poll_once(_drafting_context(provider, hostile))

    parameters = set(inspect.signature(MailProvider.create_draft).parameters)
    assert parameters == {"self", "thread_id", "body"}
    assert "recipient" not in parameters and "cc" not in parameters and "bcc" not in parameters

    assert len(provider.drafted) == 1
    thread_id, body = provider.drafted[0]
    assert thread_id == DRAFT_THREAD
    assert "attacker.example" in body, "the address is PROSE in the body, positive control"

    # The recipients the ADAPTER reported, which is the only place recipients
    # are ever decided. The injected address appears in the body text and
    # NOWHERE in the addressing, because there is no parameter that could
    # carry it there.
    ledger_rows = [row for row in ledger.read_all() if row.action == ledger.ACTION_DRAFT]
    assert len(ledger_rows) == 1
    assert "attacker.example" not in ledger_rows[0].detail


def test_a_thread_the_owner_already_replied_to_is_never_drafted_on() -> None:
    """The owner's reply IS the answer; a draft under it argues with them."""
    _approved_profile()
    provider = DraftingProvider(
        ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
        replier="owner@example.com",
    )

    summary = watcher.poll_once(_drafting_context(provider, EchoBackend()))

    assert provider.drafted == []
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE]
    assert summary.applied == 1


def test_no_backend_means_no_drafting_at_all() -> None:
    """The default posture. ``FakeProvider.create_draft`` raising is the proof."""
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    summary = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    assert summary.applied == 1
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE]


# ---------------------------------------------------------------------------
# The drafting side of #179: the ledger carve-out is scoped to the append, so
# a transport failure in `create_draft` is contained and an audit-write
# failure still aborts the round.
# ---------------------------------------------------------------------------


def test_a_create_draft_socket_error_is_not_taken_by_the_ledger_carve_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (#179) if the carve-out is spelled by TYPE at the boundary.

    ``ConnectionResetError`` is an ``OSError``, so a boundary that re-raised
    ``OSError`` broadly would abort the round on a blip in the OPTIONAL draft
    write -- after the message was already filed and receipted. The filing is
    the product; the draft is the addition, so this degrades to a filed message
    with no ACTION_DRAFT row.
    """
    _approved_profile()
    provider = DraftingProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    def _blip(thread_id: str, body: str) -> DraftResult:
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(provider, "create_draft", _blip)

    summary = watcher.poll_once(_drafting_context(provider, EchoBackend()))

    assert summary.applied == 1, "the filing already happened and must stand"
    assert summary.failed == 0
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE], (
        "no draft row may appear for a draft that was never created"
    )


def test_a_failed_draft_ledger_write_takes_the_carve_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE CARVE-OUT ITSELF, on the drafting side. Narrowing must not delete it.

    The draft EXISTS in the mailbox by the time this append runs, so a round
    that carried on would leave a provider write with no receipt. That is the
    one drafting failure the boundary may not contain.
    """
    _approved_profile()
    provider = DraftingProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    real_append = ledger.append

    def _refuse_draft_rows(name: str, action: str, *args: object, **kwargs: object) -> object:
        if action == ledger.ACTION_DRAFT:
            raise OSError(28, "No space left on device")
        return real_append(name, action, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ledger, "append", _refuse_draft_rows)

    with pytest.raises(watcher._LedgerWriteError):
        watcher.poll_once(_drafting_context(provider, EchoBackend()))

    assert len(provider.drafted) == 1, "the draft landed; that is why the abort is correct"
