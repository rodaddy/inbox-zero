"""Graph (Microsoft) cursor-reset restore tests.

Split out of ``test_reset.py``: the combined file reached 620 code lines
against this package's 500-code-line ceiling, which
``scripts/check_code_size.py`` enforces. The seam chosen for the cut is the
same one ``src/`` already uses -- ``reset_graph.py`` was split out of
``reset.py`` on the provider boundary -- so the tests now mirror the modules
they exercise: Gmail and shared reset behaviour stays in ``test_reset.py``,
and everything that drives a Graph mailbox lives here.

Covered here: the G1 Graph restore contracts (ledger-selected /move back to
the snapshot folder, an unreceipted relocation left alone, a 404 mid-run
recorded as missing, and post-move re-verification), the cross-provider
snapshot refusal, and the RD refile -> restore round trip that drives the
REAL watcher over the REAL Graph adapter, plus the dead-ledger-id loudness
test that pins the 2026-08-19 silent no-op.

Shared helpers and constants stay in ``test_reset.py`` and are imported from
it directly, which is how this suite already imports siblings (see
``_outlook_harness``).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from _outlook_harness import MOCK_TRANSPORT_SENTINEL
from _reset_harness import CONNECTOR_LABEL, INBOX_ID
from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.providers.base import ChangeSet, Container, Message
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.reset import fixture as reset
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState

# --- Graph restore (lane G1) --------------------------------------------------
#
# WHY A LANE-LOCAL TRANSPORT INSTEAD OF ``_outlook_harness.Recorder``:
# ``Recorder._move`` answers a fixed ``{"id": "moved"}`` and neither reads the
# ``destinationId`` in the request body nor changes where the message lives, and
# its message routes serve three fixed lists. A restore round trip is by
# definition stateful -- the post-restore placement re-read has to observe what
# the ``/move`` calls did, and "no /move was issued for this message" is the
# whole assertion for the ledger-driven-selection contract. A canned transport
# cannot express either. The harness is not editable by this lane (lane
# contract's harness Tightening), so ``GraphMailbox`` below WRAPS the same
# ``httpx.MockTransport`` shape locally rather than growing the shared file.
# It reuses the harness's ``MOCK_TRANSPORT_SENTINEL`` so the credential-shaped
# constant still exists in exactly one place.

RESTORE_ACTION_ID = "F_ACTION"
GRAPH_ONE = "<graph-one@example.com>"
GRAPH_TWO = "<graph-two@example.com>"


class GraphMailbox:
    """A mocked Graph mailbox whose ``/move`` calls change later reads."""

    def __init__(self, placement: dict[str, str]) -> None:
        self.placement = dict(placement)
        self.moves: list[tuple[str, str]] = []
        self.move_status: dict[str, int] = {}

    def _messages(self, folder_id: str) -> httpx.Response:
        value = [
            {"id": f"pid{index}", "internetMessageId": message_id, "parentFolderId": folder_id}
            for index, (message_id, parent) in enumerate(sorted(self.placement.items()), start=1)
            if parent == folder_id
        ]
        return httpx.Response(200, json={"value": value})

    def _move(self, request: httpx.Request) -> httpx.Response:
        provider_id = request.url.path.split("/messages/")[-1].rsplit("/", 1)[0]
        message_id = self._message_for(provider_id)
        status = self.move_status.get(message_id, 200)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "ErrorItemNotFound"}})
        destination = json.loads(request.content)["destinationId"]
        self.moves.append((message_id, destination))
        self.placement[message_id] = destination
        return httpx.Response(
            200,
            json={
                "id": provider_id,
                "internetMessageId": message_id,
                "parentFolderId": destination,
            },
        )

    def _message_for(self, provider_id: str) -> str:
        index = int(provider_id.removeprefix("pid")) - 1
        return sorted(self.placement)[index]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/move"):
            return self._move(request)
        if request.method == "GET" and "/mailFolders/" in path and path.endswith("/messages"):
            return self._messages(path.rsplit("/mailFolders/", 1)[-1].removesuffix("/messages"))
        return httpx.Response(404, json={"error": {"message": f"unmapped {request.method} {path}"}})


class RestoreGraphProvider(OutlookProvider):
    """Graph adapter with a fixed two-folder vocabulary, like ``SnapshotProvider``.

    ``list_containers`` is overridden for the same reason the snapshot fixture
    in ``test_reset.py`` overrides it: the real one resolves five well-known
    names and then walks ``childFolders`` for every folder it finds, which is a
    folder-tree contract already covered by the Outlook provider tests and pure
    noise here.
    """

    def list_containers(self) -> list[Container]:
        return [
            Container(
                id=INBOX_ID, name="Inbox", well_known="inbox", parent_id=None, exclusive=True
            ),
            Container(
                id=RESTORE_ACTION_ID,
                name=CONNECTOR_LABEL,
                well_known=None,
                parent_id=None,
                exclusive=True,
            ),
        ]


def _graph_provider(mailbox: GraphMailbox) -> OutlookProvider:
    """The real Graph adapter over the stateful lane-local transport."""
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(mailbox.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return RestoreGraphProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


def _install_graph_provider(
    monkeypatch: pytest.MonkeyPatch, mailbox: GraphMailbox
) -> OutlookProvider:
    """Install one mocked Graph provider at the reset CLI boundary."""
    provider = _graph_provider(mailbox)
    monkeypatch.setenv("INBOX_ZERO_PROVIDER", "microsoft")
    monkeypatch.setattr(cli, "get_provider", lambda *_args, **_kwargs: provider)
    return provider


def _graph_snapshot_file(provider: OutlookProvider, output: Path, expected: int = 2) -> None:
    """Take a full-scope Graph snapshot before the fixture run mutates placement."""
    count = reset.write_snapshot(provider, MailboxScope(mode="all_folders"), output)
    assert count == expected


def test_graph_restore_moves_a_filed_message_back_to_its_snapshot_folder(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An APPLIED Graph filing is undone by a /move back to the snapshot folder."""
    mailbox = GraphMailbox({GRAPH_ONE: INBOX_ID, GRAPH_TWO: INBOX_ID})
    provider = _install_graph_provider(monkeypatch, mailbox)
    snapshot = isolated_dirs / "graph-restore.json"
    _graph_snapshot_file(provider, snapshot)
    mailbox.placement[GRAPH_ONE] = RESTORE_ACTION_ID
    ledger.append("microsoft", ledger.ACTION_FILE, GRAPH_ONE, CONNECTOR_LABEL)

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert mailbox.moves == [(GRAPH_ONE, INBOX_ID)]
    assert mailbox.placement[GRAPH_ONE] == INBOX_ID
    assert "modified=1" in capsys.readouterr().out


def test_graph_restore_leaves_a_message_without_an_applied_row_untouched(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ledger selects the writes: an unreceipted relocation is reported, not moved."""
    mailbox = GraphMailbox({GRAPH_ONE: INBOX_ID, GRAPH_TWO: INBOX_ID})
    provider = _install_graph_provider(monkeypatch, mailbox)
    snapshot = isolated_dirs / "graph-unreceipted.json"
    _graph_snapshot_file(provider, snapshot)
    mailbox.placement[GRAPH_TWO] = RESTORE_ACTION_ID

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert mailbox.moves == []
    assert mailbox.placement[GRAPH_TWO] == RESTORE_ACTION_ID
    output = capsys.readouterr().out
    assert "modified=0" in output
    assert "mismatches=1" in output
    assert f"Finding: {GRAPH_TWO}" in output


def test_graph_restore_records_a_404_move_as_missing_and_continues(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A message that vanished mid-run is named, and the other one still lands."""
    mailbox = GraphMailbox({GRAPH_ONE: INBOX_ID, GRAPH_TWO: INBOX_ID})
    provider = _install_graph_provider(monkeypatch, mailbox)
    snapshot = isolated_dirs / "graph-missing.json"
    _graph_snapshot_file(provider, snapshot)
    mailbox.placement[GRAPH_ONE] = RESTORE_ACTION_ID
    mailbox.placement[GRAPH_TWO] = RESTORE_ACTION_ID
    mailbox.move_status[GRAPH_ONE] = 404
    ledger.append("microsoft", ledger.ACTION_FILE, GRAPH_ONE, CONNECTOR_LABEL)
    ledger.append("microsoft", ledger.ACTION_FILE, GRAPH_TWO, CONNECTOR_LABEL)

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert (GRAPH_TWO, INBOX_ID) in mailbox.moves
    output = capsys.readouterr().out
    assert "modified=1" in output
    assert f"Missing: {GRAPH_ONE}" in output


def test_graph_restore_reverifies_placement_and_reports_a_message_still_out_of_place(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verification re-reads the mailbox rather than trusting the move responses."""
    mailbox = GraphMailbox({GRAPH_ONE: INBOX_ID, GRAPH_TWO: INBOX_ID})
    provider = _install_graph_provider(monkeypatch, mailbox)
    snapshot = isolated_dirs / "graph-verify.json"
    _graph_snapshot_file(provider, snapshot)
    mailbox.placement[GRAPH_ONE] = RESTORE_ACTION_ID
    mailbox.placement[GRAPH_TWO] = RESTORE_ACTION_ID
    ledger.append("microsoft", ledger.ACTION_FILE, GRAPH_ONE, CONNECTOR_LABEL)

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    output = capsys.readouterr().out
    assert "mismatches=1" in output
    assert f"Finding: {GRAPH_TWO}" in output
    assert f"Finding: {GRAPH_ONE}" not in output


def test_restore_refuses_a_snapshot_from_another_provider_before_any_request(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Gmail snapshot cannot drive a Graph mailbox, and nothing is requested first."""
    mailbox = GraphMailbox({GRAPH_ONE: INBOX_ID, GRAPH_TWO: INBOX_ID})
    provider = _install_graph_provider(monkeypatch, mailbox)
    snapshot = isolated_dirs / "wrong-provider.json"
    _graph_snapshot_file(provider, snapshot)
    payload = json.loads(snapshot.read_text())
    payload["provider"] = "google"
    snapshot.write_text(json.dumps(payload))
    mailbox.moves.clear()

    with pytest.raises(reset.RestoreError, match="does not match active provider"):
        reset.restore_snapshot(provider, snapshot)

    assert mailbox.moves == []


# --- Graph refile -> restore round trip (lane RD) ------------------------------
#
# WHAT THE G1 TESTS ABOVE DO NOT COVER. Every Graph restore test above seeds the
# ledger by hand -- ``ledger.append("microsoft", ACTION_FILE, GRAPH_ONE, ...)``
# with GRAPH_ONE being the INTERNET message id. That is the id restore's
# id-bridge is built to resolve, so the bridge is handed its own answer and the
# round trip closes. The WATCHER never writes that id.
#
# What the watcher writes is the id it passed to ``file_message`` -- the PRE-move
# provider id -- because it discards the adapter's return value
# (``watcher.py``:405, 437, 439). Graph's /move is copy-then-delete, so that id
# is dead the moment the filing lands. ``reset._details_by_message`` then finds
# it in neither the snapshot (which is keyed by internet id) nor
# ``provider_ids`` (which maps only ids that still EXIST), hits its ``continue``,
# and drops the message from the restore set entirely.
#
# The result is the live failure of 2026-08-19: 54 filed messages, 54
# unrestorable, ``modified=0``, exit 0. The tests below drive the REAL watcher
# through the REAL Graph adapter so the ledger row is the one production writes,
# which is the only arrangement in which that defect is visible.


class RefileMailbox(GraphMailbox):
    """``GraphMailbox`` that reassigns the provider id on /move, as Graph does.

    TWO deltas from ``GraphMailbox``, and the first is the one that matters.

    1. ``/move`` answers with a NEW ``id``. Graph's /move is copy-then-delete:
       the destination holds a new entity and the id that was moved is dead.
       ``GraphMailbox._move`` echoes the REQUESTED id straight back, so a
       message keeps its id across a move for the whole G1 suite -- which is
       exactly why those tests stayed green while the live tenant produced 54
       unrestorable messages on 2026-08-19. A mock that cannot express the
       reassignment cannot fail on it.
    2. ``GET /me/mailFolders`` is served, because ``file_message`` resolves its
       destination through it. The G1 tests never file through the adapter.

    Both live in a SUBCLASS rather than in ``GraphMailbox`` itself, so the
    tests above keep the exact transport surface they were written against.
    """

    def __init__(self, placement: dict[str, str]) -> None:
        super().__init__(placement)
        self.reassigned: dict[str, str] = {}
        """Live provider id per internet id, rewritten by every /move."""

    def _provider_id(self, message_id: str) -> str:
        """The id this message currently answers to."""
        default = f"pid{sorted(self.placement).index(message_id) + 1}"
        return self.reassigned.get(message_id, default)

    def _message_for(self, provider_id: str) -> str:
        for message_id in self.placement:
            if self._provider_id(message_id) == provider_id:
                return message_id
        return super()._message_for(provider_id)

    def _messages(self, folder_id: str) -> httpx.Response:
        """Serve each message at its CURRENT id, not its original one."""
        value = [
            {
                "id": self._provider_id(message_id),
                "internetMessageId": message_id,
                "parentFolderId": parent,
            }
            for message_id, parent in sorted(self.placement.items())
            if parent == folder_id
        ]
        return httpx.Response(200, json={"value": value})

    def _move(self, request: httpx.Request) -> httpx.Response:
        provider_id = request.url.path.split("/messages/")[-1].rsplit("/", 1)[0]
        message_id = self._message_for(provider_id)
        status = self.move_status.get(message_id, 200)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "ErrorItemNotFound"}})
        destination = json.loads(request.content)["destinationId"]
        self.moves.append((message_id, destination))
        self.placement[message_id] = destination
        new_id = f"{provider_id}-moved"
        self.reassigned[message_id] = new_id
        return httpx.Response(
            200,
            json={
                "id": new_id,
                "internetMessageId": message_id,
                "parentFolderId": destination,
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/me/mailFolders"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": INBOX_ID, "displayName": "Inbox"},
                        {"id": RESTORE_ACTION_ID, "displayName": CONNECTOR_LABEL},
                    ]
                },
            )
        return super().handle(request)


class RefileClassifier:
    """Sends every message to the connector folder, deterministically."""

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        return Classification(
            folder=CONNECTOR_LABEL,
            confidence=1.0,
            reason="refile fixture",
            deterministic=True,
        )


def _graph_watch_once(provider: OutlookProvider, mailbox: GraphMailbox) -> None:
    """File every inbox message through the REAL watcher over the REAL adapter.

    ``poll_changes`` is bypassed with a scripted ``ChangeSet`` rather than
    given a delta route on the mailbox: the contract under test is the
    RECEIPT the watcher writes after ``file_message`` returns, and the change
    feed that delivered the message is a separate seam already covered by
    ``test_outlook_delta.py``. The ids in the scripted set are the mailbox's
    own live provider ids, so the /move calls are real.
    """
    inbox = sorted(
        message_id for message_id, parent in mailbox.placement.items() if parent == INBOX_ID
    )
    messages = tuple(
        Message(
            id=mailbox._provider_id(message_id),
            thread_id="t1",
            sender="someone@example.com",
            subject="Refile fixture",
            received="2026-08-19T00:00:00Z",
            internet_message_id=message_id,
            containers=(INBOX_ID,),
        )
        for message_id in inbox
    )
    context = watcher.RoundContext(
        provider=provider,
        scope=MailboxScope(mode="all_folders"),
        taxonomy=default_taxonomy(),
        state=WatchState(),
        classifier=RefileClassifier(),
    )
    provider.poll_changes = lambda *_a, **_k: ChangeSet(  # type: ignore[method-assign]
        messages=messages, cursor="refile-cursor"
    )
    summary = watcher.poll_once(context)
    assert summary.applied == len(messages)


def test_a_watcher_filed_graph_message_is_restored_by_the_snapshot_it_came_from(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole round trip over real code: watcher files it, restore puts it back.

    This is the live 2026-08-19 failure in miniature. The receipt the watcher
    wrote must still name a message restore can find, or the fixture silently
    undoes nothing.
    """
    mailbox = RefileMailbox({GRAPH_ONE: INBOX_ID})
    provider = _install_graph_provider(monkeypatch, mailbox)
    snapshot = isolated_dirs / "graph-refile-restore.json"
    _graph_snapshot_file(provider, snapshot, expected=1)

    _graph_watch_once(provider, mailbox)
    assert mailbox.placement[GRAPH_ONE] == RESTORE_ACTION_ID

    assert cli.main(["reset", "restore", "--from", str(snapshot)]) == 0

    assert mailbox.placement[GRAPH_ONE] == INBOX_ID
    assert "modified=1" in capsys.readouterr().out


def test_restore_exits_nonzero_when_it_had_rows_to_undo_and_undid_nothing(
    isolated_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A DEAD ledger id undoes zero of N receipted writes, and must not exit 0.

    This pins the no-op case by its cause: an APPLIED row whose ``message_id``
    resolves in NEITHER the snapshot (keyed by internet id) nor the live
    mailbox (which only maps ids that still exist). That is byte-for-byte the
    shape the pre-fix watcher wrote on 2026-08-19 -- it receipted the pre-move
    provider id, and Graph's copy-then-delete /move had already killed it.

    The row is seeded DIRECTLY rather than driven through the watcher: the
    watcher now receipts a live id, so a round trip through it restores
    successfully and cannot reach this path. The message is placed out of its
    snapshot folder by hand so a mismatch is genuinely outstanding.

    ``applied_rows > 0`` with ``modified == 0`` and mismatches outstanding is
    the exact shape of the live run: the operator asked for their mailbox back,
    got none of it, and the command reported success. A restore that restores
    nothing while findings remain has to be loud, because the exit code is what
    a script -- and the overnight runbook -- actually reads.
    """
    mailbox = RefileMailbox({GRAPH_ONE: INBOX_ID})
    provider = _install_graph_provider(monkeypatch, mailbox)
    snapshot = isolated_dirs / "graph-silent-restore.json"
    _graph_snapshot_file(provider, snapshot, expected=1)

    mailbox.placement[GRAPH_ONE] = RESTORE_ACTION_ID
    ledger.append("microsoft", ledger.ACTION_FILE, "pid-dead-premove", CONNECTOR_LABEL)

    exit_code = cli.main(["reset", "restore", "--from", str(snapshot)])

    output = capsys.readouterr().out
    assert "applied_rows=1" in output
    assert "modified=0" in output
    assert "missing=0" in output
    assert "mismatches=1" in output
    assert exit_code != 0
