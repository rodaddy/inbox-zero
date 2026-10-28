"""``build_rounds`` must hand the drafting pair to the round, or neither (#76).

The defect this pins: ``watch_accounts.build_rounds`` built its ``RoundContext``
without ``draft_backend`` and without ``routing``, and ``round._draft`` refuses
to compose when either is ``None`` (``services/watch/round.py:2053``). So an
account configured ``file_read_draft`` with ``drafts_enabled`` for its provider
still got a watcher whose drafting limb could never run -- the kit's own
``docs/KIT-BOUNDARY-ANALYSIS.md:150`` records the same absence as "by accident
of wiring rather than by design".

WHY BOTH DIRECTIONS ARE ASSERTED. A drafts-ON account proving the pair arrives
is only half the contract: the other half is that a drafts-OFF account gets
``None`` for BOTH, because ``round`` reads an absent backend as the deployment
posture rather than as an oversight. A test that only pinned the ON direction
would pass against an implementation that injected the pair unconditionally,
which is the widening failure ``_account_specs`` exists to make impossible.

THE ASSERTIONS ARE ON PRESENCE, NOT ON IDENTITY. Which composer a deployment
builds is the composer factory's own question; what this file is about is that
the assembly step stops dropping it on the floor.
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.accounts import AccountSettings
from inbox_zero_connector.apps.cli import watch_accounts
from inbox_zero_connector.config import Settings, VoiceRouting
from inbox_zero_connector.providers.base import MailProvider
from inbox_zero_connector.services.watch.round import RoundContext


class _StubProvider(MailProvider):
    """Held by the context, never called: assembly touches no mailbox.

    A real ``MailProvider`` subclass rather than a duck: ``RoundContext.provider``
    is pydantic-validated ``is_instance_of``, so a plain object fails
    construction and the test would report a harness error where the wiring
    answer belongs. Every method raises, so a future change that makes assembly
    reach the mailbox fails loudly here instead of passing against a fake.
    """

    name = "google"

    def _unreachable(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("build_rounds must not call the provider")

    authenticate = _unreachable
    create_draft = _unreachable
    create_self_draft = _unreachable
    file_message = _unreachable
    get_thread = _unreachable
    list_containers = _unreachable
    list_inbox = _unreachable
    poll_changes = _unreachable
    read_sent_sample = _unreachable
    set_important = _unreachable
    tag_message = _unreachable


def _account(address: str, *, provider: str, posture: str) -> AccountSettings:
    """One configured account. ``drafts`` follows the posture, as a real one does."""
    return AccountSettings(
        address=address,
        provider=provider,
        owner_domain=address.split("@", maxsplit=1)[1],
        posture=posture,
        drafts=posture == "file_read_draft",
    )


def _settings_with(*, posture: str, provider: str, drafts_enabled: bool) -> Settings:
    """Settings carrying one account whose drafts answer is the thing under test.

    The INSTALL-level ``posture`` is permissive on purpose, matching
    ``tests/services/watch/test_multi_account_round.py``: these assertions are
    about the per-account answer reaching the context, so the install-level gate
    must not be what produces the ``None``.
    """
    return Settings(
        accounts=(_account("owner@example.com", provider=provider, posture=posture),),
        drafts_enabled={provider: drafts_enabled},
        posture="file_read_draft",
        # A ROUTING TABLE IS PART OF A DRAFTS-ON INSTALL, so the fixture carries
        # one. Without it ``connector_config.routing`` is ``None`` for every
        # case and the routing assertions below could never distinguish "the
        # posture withheld it" from "this fixture never configured one" -- they
        # would pass on the OFF case and fail on the ON case for a reason that
        # has nothing to do with the wiring under test. Supplying it
        # unconditionally is what makes the drafts-OFF ``None`` evidence that
        # the POSTURE produced it.
        voice_routing=VoiceRouting(owner_domain="example.com"),
    )


class _StubClassifier:
    """What ``factory.from_settings`` hands back: a backend that can compose.

    THE FIXTURE HAD TO GROW A COMPOSE METHOD, and the reason is the shape of
    the fix rather than a concession to it. ``build_rounds`` guards the
    injection with ``isinstance(classifier, DraftComposer)``, because the
    factory's return type is ``OpenAICompatibleClassifier | None`` and a
    rule-only install genuinely has no backend. The previous ``lambda: None``
    stood in for a real classifier, so under that guard it now models a
    rule-only install -- a different deployment from the one these tests are
    about, and one for which ``draft_backend is None`` is the CORRECT answer.

    ``DraftComposer`` is structural, so this plain object satisfies it with no
    import and no registration. It is never called: assembly wires the seam and
    composes nothing, which is why the method raises.
    """

    def classify(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("build_rounds must not classify")

    def compose(self, prompt: str, voice: object) -> str:
        raise AssertionError("build_rounds must not compose")


def _only_context(monkeypatch: pytest.MonkeyPatch, active: Settings) -> RoundContext:
    """Build the rounds for ``active`` and return the single context produced."""
    monkeypatch.setattr(watch_accounts.factory, "from_settings", lambda _active: _StubClassifier())
    monkeypatch.setattr(watch_accounts, "get_provider", lambda *args, **kwargs: _StubProvider())
    rounds, _checkpoint = watch_accounts.build_rounds(active)
    assert len(rounds) == 1, "this fixture configures exactly one account"
    return rounds[0].context


def test_a_drafts_on_account_gets_both_halves_of_the_drafting_pair(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    """Drafts on: the composer AND the routing arrive, because ``_draft`` needs both."""
    context = _only_context(
        monkeypatch,
        _settings_with(posture="file_read_draft", provider="google", drafts_enabled=True),
    )
    assert context.draft_backend is not None, "drafts are on and no composer was injected"
    assert context.routing is not None, "drafts are on and no voice routing was injected"


def test_a_drafts_off_account_gets_neither_half(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    """Drafts off: BOTH are ``None``, which is how ``round`` reads the posture."""
    context = _only_context(
        monkeypatch,
        _settings_with(posture="file_only", provider="google", drafts_enabled=False),
    )
    assert context.draft_backend is None, "drafts are off and a composer was injected anyway"
    assert context.routing is None, "drafts are off and voice routing was injected anyway"


def test_a_rule_only_install_gets_no_composer_even_with_drafts_on(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    """No model backend configured: ``draft_backend`` is ``None``, routing still arrives.

    The guard's other branch, and the one a bare ``draft_backend=classifier``
    would get wrong. ``factory.from_settings`` returns ``None`` for an install
    with no ``base_url``, and injecting that would satisfy the ON test above
    while handing ``round`` a backend with no ``compose`` -- a failure deferred
    to the first message instead of an install that simply files.

    Routing is asserted PRESENT to show the two are answered independently:
    this is a missing backend, not a drafts-off posture.
    """
    monkeypatch.setattr(watch_accounts.factory, "from_settings", lambda _active: None)
    monkeypatch.setattr(watch_accounts, "get_provider", lambda *args, **kwargs: _StubProvider())
    rounds, _checkpoint = watch_accounts.build_rounds(
        _settings_with(posture="file_read_draft", provider="google", drafts_enabled=True)
    )
    context = rounds[0].context
    assert context.draft_backend is None, "no model backend was configured to compose with"
    assert context.routing is not None, "the posture is drafts-on; only the backend is absent"
