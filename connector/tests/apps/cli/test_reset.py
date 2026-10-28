"""Cursor-reset fixture tests.

The CLI boundary is exercised so the tests prove state is loaded, changed, and
saved without touching a real mailbox. The autouse ``isolated_dirs`` fixture
keeps every state file inside pytest's temporary data directory.

Gmail and provider-shared reset behaviour only. The Graph restore tests were
split into ``test_reset_graph.py`` when the combined file passed the package's
500-code-line ceiling (``scripts/check_code_size.py``); the cut follows the
provider seam ``src/`` already uses, where ``reset_graph.py`` split out of
``reset.py``. Helpers and constants shared by both files stay HERE and are
imported from this module by the Graph file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from _gmail_harness import FakeMessages, FakeService, FakeUsers, _Call, _message
from _outlook_harness import MOCK_TRANSPORT_SENTINEL
from _reset_harness import CONNECTOR_LABEL, INBOX_ID
from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.config import load_settings
from inbox_zero_connector.providers.base import Container, Message
from inbox_zero_connector.providers.gmail.provider import GmailProvider
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.reset import fixture as reset
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState, state_path

FIRST = "folder-first"
SECOND = "folder-second"
THIRD = "folder-third"
CONTAINERS = (FIRST, SECOND, THIRD)
TOKENS = {
    FIRST: "cursor-first",
    SECOND: "cursor-second",
    THIRD: "cursor-third",
}


def _store_three_cursors() -> str:
    provider = load_settings().provider
    state = WatchState()
    for container_id, token in TOKENS.items():
        state.record_cursor(container_id, token)
        state.mark_seen(container_id, f"seen-{container_id}")
    state.save(provider)
    return provider


def test_reset_one_cursor_keeps_other_cursors_and_every_replay_window() -> None:
    """One reset cold-starts only its stream and increments only its generation."""
    provider = _store_three_cursors()

    assert cli.main(["reset", "--container", SECOND]) == 0

    reloaded = WatchState.load(provider)
    assert reloaded.cursor_for(SECOND) is None
    for container_id in (FIRST, THIRD):
        cursor = reloaded.cursor_for(container_id)
        assert cursor is not None
        assert cursor.token == TOKENS[container_id]
    for container_id in CONTAINERS:
        assert reloaded.seen_message(container_id, f"seen-{container_id}")
    assert reloaded.generation_for(SECOND) == 1
    assert reloaded.generation_for(FIRST) == 0
    assert reloaded.generation_for(THIRD) == 0


def test_all_cursors_resets_every_stream_and_keeps_replay_windows() -> None:
    """The all-cursors fixture cold-starts every stored stream without deleting state."""
    provider = _store_three_cursors()

    assert cli.main(["reset", "--all-cursors"]) == 0

    reloaded = WatchState.load(provider)
    assert state_path(provider).exists()
    for container_id in CONTAINERS:
        assert reloaded.cursor_for(container_id) is None
        assert reloaded.generation_for(container_id) == 1
        assert reloaded.seen_message(container_id, f"seen-{container_id}")


SUBJECT_SENTINEL = "subject-must-never-reach-the-snapshot"
SENDER_SENTINEL = "sender-must-never-reach-the-snapshot@example.com"


def _snapshot_message(index: int) -> dict[str, Any]:
    """One Graph response carrying content the snapshot must discard."""
    return {
        "id": f"provider-id-{index}",
        "internetMessageId": f"<message-{index}@example.com>",
        "parentFolderId": INBOX_ID,
        "subject": f"{SUBJECT_SENTINEL}-{index}",
        "from": {"emailAddress": {"address": SENDER_SENTINEL}},
    }


class SnapshotTransport:
    """Serve one message per Graph page so the test requires pagination."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith(f"/mailFolders/{INBOX_ID}/messages"):
            return httpx.Response(404, json={"error": {"message": "unmapped read"}})
        offset = int(request.url.params.get("$skiptoken", "0"))
        page: dict[str, Any] = {"value": self.messages[offset : offset + 1]}
        if offset + 1 < len(self.messages):
            page["@odata.nextLink"] = (
                f"{GRAPH_BASE}/me/mailFolders/{INBOX_ID}/messages?$skiptoken={offset + 1}"
            )
        return httpx.Response(200, json=page)


class SnapshotProvider(OutlookProvider):
    """Graph adapter with a fixed in-scope folder set and mocked message transport."""

    def list_containers(self) -> list[Container]:
        return [
            Container(
                id=INBOX_ID,
                name="Inbox",
                well_known="inbox",
                parent_id=None,
                exclusive=True,
            )
        ]


def _provider(messages: list[dict[str, Any]]) -> OutlookProvider:
    transport = SnapshotTransport(messages)
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(transport.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return SnapshotProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


def _install_snapshot_provider(
    monkeypatch: pytest.MonkeyPatch, messages: list[dict[str, Any]]
) -> None:
    provider = _provider(messages)
    monkeypatch.setenv("INBOX_ZERO_PROVIDER", "microsoft")
    monkeypatch.setattr(cli, "get_provider", lambda *_args, **_kwargs: provider)


def test_snapshot_records_only_internet_id_and_containers_by_internet_id(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second page lands, while content returned by Graph cannot enter the file."""
    _install_snapshot_provider(monkeypatch, [_snapshot_message(1), _snapshot_message(2)])
    output = isolated_dirs / "snapshot.json"

    assert cli.main(["reset", "snapshot", "--out", str(output)]) == 0

    expected = {
        "provider": "microsoft",
        "ledger_entries": 0,
        "messages": {
            "<message-1@example.com>": {
                "internet_message_id": "<message-1@example.com>",
                "containers": [INBOX_ID],
            },
            "<message-2@example.com>": {
                "internet_message_id": "<message-2@example.com>",
                "containers": [INBOX_ID],
            },
        },
    }
    text = output.read_text()
    assert json.loads(text) == expected
    assert SUBJECT_SENTINEL not in text
    assert SENDER_SENTINEL not in text


def test_snapshot_over_limit_exits_one_without_writing_a_prefix(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One message beyond the cap is a refusal, never a plausible partial file."""
    _install_snapshot_provider(monkeypatch, [_snapshot_message(1), _snapshot_message(2)])
    output = isolated_dirs / "truncated.json"

    assert cli.main(["reset", "snapshot", "--out", str(output), "--limit", "1"]) == 1

    assert not output.exists()
    assert "Snapshot limit 1 exceeded" in capsys.readouterr().out


CONNECTOR_LABEL_ID = "LBL_CONNECTOR"
HUMAN_LABEL_ID = "LBL_HUMAN"
INTERNET_ONE = "<restore-one@example.com>"
INTERNET_TWO = "<restore-two@example.com>"
INTERNET_EXTRA = "<restore-extra@example.com>"


class MutableMessages(FakeMessages):
    """Apply mocked Gmail modify bodies to the in-memory mailbox."""

    def modify(self, **kwargs: Any) -> _Call:
        self._log.append(("messages.modify", kwargs))
        message = self._store["messages"][kwargs["id"]]
        labels = list(message.get("labelIds", []) or [])
        body = kwargs["body"]
        removed = frozenset(body.get("removeLabelIds", []))
        labels = [label for label in labels if label not in removed]
        for label in body.get("addLabelIds", []):
            if label not in labels:
                labels.append(label)
        message["labelIds"] = labels
        return _Call(self._log, "messages.modify", {"id": kwargs["id"]})


class MutableUsers(FakeUsers):
    """Publish the stateful message resource through the fake Gmail service."""

    def messages(self) -> MutableMessages:
        return MutableMessages(self._log, self._store)


class MutableService(FakeService):
    """A mocked Gmail transport whose writes affect later reads."""

    def users(self) -> MutableUsers:
        return MutableUsers(self.log, self._store)


class RestoreProvider(GmailProvider):
    """The real Gmail adapter over the stateful mocked transport."""

    def __init__(self, store: dict[str, Any]) -> None:
        super().__init__(service=MutableService(store))


class RestoreClassifier:
    """A deterministic watch classifier for the restore round trip."""

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        return Classification(
            folder=CONNECTOR_LABEL,
            confidence=1.0,
            reason="restore fixture",
            deterministic=True,
        )


def _gmail_raw(provider_id: str, internet_id: str) -> dict[str, Any]:
    """One mocked Gmail message with distinct provider and internet ids."""
    raw = _message(provider_id, "Restore fixture", labels=["INBOX", "UNREAD"])
    for header in raw["payload"]["headers"]:
        if header["name"] == "Message-Id":
            header["value"] = internet_id
    return raw


def _internet_id(raw: dict[str, Any]) -> str:
    """Read the stable id from a mocked raw Gmail response."""
    headers = raw["payload"]["headers"]
    return next(str(header["value"]) for header in headers if header["name"] == "Message-Id")


def _gmail_store() -> dict[str, Any]:
    """Two-message mocked Gmail mailbox with both connector and human labels."""
    messages = {
        "provider-one": _gmail_raw("provider-one", INTERNET_ONE),
        "provider-two": _gmail_raw("provider-two", INTERNET_TWO),
    }
    return {
        "list": {"messages": [{"id": provider_id} for provider_id in messages]},
        "sent_list": {"messages": []},
        "messages": messages,
        "thread": {"id": "unused", "messages": []},
        "labels": [
            {"id": "INBOX", "name": "INBOX"},
            {"id": "UNREAD", "name": "UNREAD"},
            {"id": CONNECTOR_LABEL_ID, "name": CONNECTOR_LABEL},
            {"id": HUMAN_LABEL_ID, "name": "Human"},
        ],
        "history_id": "restore-cursor",
        "history": {"historyId": "restore-cursor", "history": []},
    }


def _install_restore_provider(
    monkeypatch: pytest.MonkeyPatch,
    store: dict[str, Any],
) -> RestoreProvider:
    """Install one mocked Gmail provider at the reset CLI boundary."""
    provider = RestoreProvider(store)
    monkeypatch.setenv("INBOX_ZERO_PROVIDER", "google")
    monkeypatch.setattr(cli, "get_provider", lambda *_args, **_kwargs: provider)
    return provider


def _take_gmail_snapshot(provider: GmailProvider, output: Path) -> None:
    """Take a full-scope fixture snapshot before the mocked run starts."""
    count = reset.write_snapshot(provider, MailboxScope(mode="all_folders"), output)
    assert count == 2


def _watch_once(provider: RestoreProvider) -> None:
    """Run the real watcher boundary so labels and APPLIED receipts are produced."""
    context = watcher.RoundContext(
        provider=provider,
        scope=MailboxScope(mode="all_folders"),
        taxonomy=default_taxonomy(),
        state=WatchState(),
        classifier=RestoreClassifier(),
    )
    summary = watcher.poll_once(context)
    assert summary.applied == 2


def _placements(store: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Mailbox placement keyed only by internet_message_id."""
    return {
        _internet_id(raw): frozenset(str(label) for label in raw["labelIds"])
        for raw in store["messages"].values()
    }


def test_restore_returns_watched_and_filed_messages_to_snapshot_placement(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ledger-selected undo restores every message by internet id."""
    store = _gmail_store()
    provider = _install_restore_provider(monkeypatch, store)
    snapshot = isolated_dirs / "restore.json"
    _take_gmail_snapshot(provider, snapshot)
    expected = _placements(store)

    _watch_once(provider)

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert _placements(store) == expected
    output = capsys.readouterr().out
    assert "missing=0 mismatches=0" in output
    assert "Finding:" not in output


def test_restore_logs_and_skips_a_message_missing_at_restore(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A deleted fixture message is named and skipped without failing the command."""
    store = _gmail_store()
    provider = _install_restore_provider(monkeypatch, store)
    snapshot = isolated_dirs / "missing.json"
    _take_gmail_snapshot(provider, snapshot)
    ledger.append("google", ledger.ACTION_FILE, "provider-one", CONNECTOR_LABEL)
    del store["messages"]["provider-one"]
    store["list"]["messages"] = [{"id": "provider-two"}]

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    output = capsys.readouterr().out
    assert "missing=1" in output
    assert f"Missing: {INTERNET_ONE}" in output


def test_restore_leaves_a_present_message_absent_from_snapshot_untouched(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new arrival is outside the snapshot even when a later receipt names it."""
    store = _gmail_store()
    provider = _install_restore_provider(monkeypatch, store)
    snapshot = isolated_dirs / "new-arrival.json"
    _take_gmail_snapshot(provider, snapshot)
    extra = _gmail_raw("provider-extra", INTERNET_EXTRA)
    extra["labelIds"].append(CONNECTOR_LABEL_ID)
    store["messages"]["provider-extra"] = extra
    store["list"]["messages"].append({"id": "provider-extra"})
    ledger.append("google", ledger.ACTION_FILE, "provider-extra", CONNECTOR_LABEL)
    before = frozenset(extra["labelIds"])

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert frozenset(extra["labelIds"]) == before


def test_restore_ignores_receipts_before_the_snapshot_boundary(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old receipt cannot authorize removal during a later run window."""
    store = _gmail_store()
    provider = _install_restore_provider(monkeypatch, store)
    ledger.append("google", ledger.ACTION_FILE, "provider-one", CONNECTOR_LABEL)
    snapshot = isolated_dirs / "ledger-window.json"
    _take_gmail_snapshot(provider, snapshot)
    store["messages"]["provider-one"]["labelIds"].append(CONNECTOR_LABEL_ID)

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert CONNECTOR_LABEL_ID in store["messages"]["provider-one"]["labelIds"]


def test_restore_does_not_undo_a_failed_ledger_row(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED receipts changed nothing and therefore authorize no restore write."""
    store = _gmail_store()
    provider = _install_restore_provider(monkeypatch, store)
    snapshot = isolated_dirs / "failed-row.json"
    _take_gmail_snapshot(provider, snapshot)
    store["messages"]["provider-one"]["labelIds"].append(CONNECTOR_LABEL_ID)
    ledger.append(
        "google",
        ledger.ACTION_FILE,
        "provider-one",
        CONNECTOR_LABEL,
        outcome=ledger.failed("ProviderError"),
    )

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert CONNECTOR_LABEL_ID in store["messages"]["provider-one"]["labelIds"]


def test_restore_keeps_a_human_label_applied_outside_the_ledger(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the connector label is removed; the human edit survives and is reported."""
    store = _gmail_store()
    provider = _install_restore_provider(monkeypatch, store)
    snapshot = isolated_dirs / "human-edit.json"
    _take_gmail_snapshot(provider, snapshot)
    provider.tag_message("provider-one", CONNECTOR_LABEL)
    ledger.append("google", ledger.ACTION_FILE, "provider-one", CONNECTOR_LABEL)
    store["messages"]["provider-one"]["labelIds"].append(HUMAN_LABEL_ID)

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    labels = frozenset(store["messages"]["provider-one"]["labelIds"])
    assert CONNECTOR_LABEL_ID not in labels
    assert HUMAN_LABEL_ID in labels
    output = capsys.readouterr().out
    assert "mismatches=1" in output
    assert f"Finding: {INTERNET_ONE}" in output


class _UnsupportedProvider:
    """The smallest object that is neither Gmail nor Graph. Kept local on purpose.

    A restore refusal for an unknown adapter must not need a real provider to
    prove: the point is that ``restore_snapshot`` decides on TYPE before it
    touches a mailbox, so anything that can carry a ``name`` is enough.
    """

    name = "unsupported"

    def list_containers(self) -> list[Container]:
        raise AssertionError("restore must refuse before reading any container")


def test_restore_refuses_an_unsupported_provider_before_any_request(
    isolated_dirs: Path,
) -> None:
    """An adapter with no restore implementation is refused, not partially run."""
    snapshot = isolated_dirs / "unsupported.json"
    snapshot.write_text(
        json.dumps({"provider": "unsupported", "ledger_entries": 0, "messages": {}})
    )

    with pytest.raises(reset.RestoreError, match="does not support provider type"):
        reset.restore_snapshot(_UnsupportedProvider(), snapshot)  # type: ignore[arg-type]
