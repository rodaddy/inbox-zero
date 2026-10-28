"""A ``watch --once`` pass that polled ZERO streams is a failure, not a success.

The defect this pins (#175): ``run_pass`` with a scope that matches nothing
returns ``PassResult(streams=())`` -- ``errors`` is 0, there are no summaries,
and the exit contract fell through to 0. A mistyped ``only_listed`` folder name
therefore reported "mailbox checked, all clean" to cron forever while nothing
was ever read.

Zero streams means nothing was watched. Nothing watched cannot be reported
clean, for the same reason ``run()`` returning ``None`` cannot (#160).
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.apps.cli import watch_accounts
from inbox_zero_connector.providers.base import MailProvider
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch.round import RoundContext


class StubProvider(MailProvider):
    """Held by the context, never called: the loop that would use it is patched out."""

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


@pytest.fixture
def _wired_watch(monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None) -> None:
    monkeypatch.setattr(
        watch_accounts.factory, "from_settings", lambda _active: SentinelClassifier()
    )
    monkeypatch.setattr(watch_accounts, "get_provider", lambda *args, **kwargs: StubProvider())


def test_watch_once_exits_non_zero_when_no_stream_was_polled(
    monkeypatch: pytest.MonkeyPatch,
    _wired_watch: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty ``streams`` tuple is a scope that matched nothing (#175)."""

    def _no_streams(context: RoundContext, **kwargs: Any) -> watchloop.PassResult:
        return watchloop.PassResult(streams=())

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _no_streams)
    assert cli.main(["watch", "--once"]) == 1
    assert "no mail stream" in capsys.readouterr().err.lower()


def test_the_zero_stream_message_names_the_scope_setting(
    monkeypatch: pytest.MonkeyPatch,
    _wired_watch: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator has to be told WHICH setting selected nothing."""

    def _no_streams(context: RoundContext, **kwargs: Any) -> watchloop.PassResult:
        return watchloop.PassResult(streams=())

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _no_streams)
    cli.main(["watch", "--once"])
    assert "only_listed" in capsys.readouterr().err
