"""``watch`` must hand the configured model backend to the round (#115).

The defect this pins: ``_cmd_watch`` built its ``RoundContext`` without a
``classifier``, so ``ClassifierSettings`` could be fully configured and the
watcher would still fall back to the rule-only layer forever. The assertion is
narrow on purpose -- that the object the factory returns is the object the
round receives -- because everything about WHICH backend gets built is the
factory's own test file.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.apps.cli import watch_accounts
from inbox_zero_connector.providers.base import MailProvider
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch.accounts_pass import AccountRound
from inbox_zero_connector.services.watch.round import RoundContext, RoundState, RoundSummary


class StubProvider(MailProvider):
    """Held by the context, never called: the loop that would use it is patched out.

    Every abstract method raises rather than returning a plausible empty value,
    so a future change that makes ``_cmd_watch`` touch the mailbox fails loudly
    here instead of quietly passing against a fake.
    """

    name = "google"

    def _unreachable(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("_cmd_watch must not call the provider")

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


class SentinelClassifier:
    """A structural ``Classifier``. A bare ``object()`` fails RoundContext validation."""

    def classify(self, message: Any, folders: tuple[str, ...]) -> Classification:
        return Classification(folder="", confidence=0.0, reason="sentinel", deterministic=False)


def _clean_pass() -> watchloop.PassResult:
    """One stream that polled and raised nothing. Scenery for the wiring assertion."""
    return watchloop.PassResult(
        streams=(watchloop.StreamResult(None, RoundState.COMMITTED.name, _summary()),)
    )


def _summary() -> RoundSummary:
    """A completed round that saw nothing and failed nothing."""
    return RoundSummary(
        container_id=None,
        state=RoundState.COMMITTED,
        seen=0,
        skipped_out_of_scope=0,
        skipped_unenumerated_container=0,
        skipped_already_seen=0,
        classified=0,
        applied=0,
        failed=0,
        cursor_stored=True,
        resynced=False,
    )


def test_watch_context_carries_the_factory_classifier(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    sentinel = SentinelClassifier()
    seen: list[RoundContext] = []

    monkeypatch.setattr(watch_accounts.factory, "from_settings", lambda _active: sentinel)
    monkeypatch.setattr(watch_accounts, "get_provider", lambda *args, **kwargs: StubProvider())

    def _capture(context: Sequence[AccountRound], **kwargs: Any) -> watchloop.PassResult:
        # `watch` hands the loop the install's ACCOUNT ROUNDS now (#69), so the
        # context this test is about sits one level in. An install with no
        # configured accounts yields exactly one round, which is what this
        # fixture's settings produce.
        seen.extend(entry.context for entry in context)
        return _clean_pass()

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _capture)

    # The stub RETURNS A CLEAN PASS THAT POLLED ONE STREAM rather than ``None``
    # or an empty tuple, both of which it has been at different times. Under
    # the corrected ``--once`` contract ``None`` means no pass ran (#160) and
    # an empty ``streams`` means the scope matched nothing (#175); each is
    # itself exit 1, so either stub would assert the wiring through a status
    # that no longer describes wiring. What this test is about is the
    # classifier reaching the context; the pass it hands back is scenery and
    # has to be the clean one for the status to stay a wiring signal.
    assert cli.main(["watch", "--once"]) == 0
    assert seen, "watch never reached the loop"
    assert seen[0].classifier is sentinel
