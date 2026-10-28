"""Shared fakes for the watch-engine tests. Imported by name via the conftest sys.path insert, the same way _gmail_harness is."""

from __future__ import annotations

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
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.triage.taxonomy import Taxonomy, default_taxonomy
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState

INBOX_ID = "Label_INBOX"
PRIVILEGED_ID = "Label_Clients"
FINAL_CURSOR = "history-2001"
# S105 matches the NAME "token", not the value: a continuation handle is an
# opaque pagination cursor, not a credential. Same exemption and reasoning as
# tests/test_gmail_provider.py.
PAGE_TOKEN = "page-2"  # ruff: ignore[hardcoded-password-string]
FOLDER = "Business-Action"
"""A folder that IS in the installed taxonomy.

A name outside it would be coerced to ``Review`` by ``classify``, and these
tests would then be asserting the coercion invariant instead of the round --
which is real behaviour, proven separately in ``test_classify.py``.
"""


class FakeProvider(MailProvider):
    """A mailbox that returns a scripted ChangeSet and records every write.

    Follows the ``SpyProvider`` pattern in ``test_server_tools.py``: the
    unused half of the interface raises, so a test that accidentally reaches
    an unimplemented capability fails loudly instead of reading as a pass.
    """

    name = "google"

    def __init__(
        self,
        changes: ChangeSet,
        *,
        containers: tuple[Container, ...] = (),
        file_error: Exception | None = None,
    ) -> None:
        self.changes = changes
        self.containers = containers or (
            Container(
                id=INBOX_ID,
                name="Inbox",
                well_known="inbox",
                parent_id=None,
                exclusive=False,
            ),
            Container(
                id=PRIVILEGED_ID,
                name="Client Matters",
                well_known=None,
                parent_id=None,
                exclusive=False,
            ),
        )
        self.file_error = file_error
        self.filed: list[tuple[str, str]] = []
        self.polled: list[str | None] = []
        self.polled_containers: list[str | None] = []
        """The container named on each poll, in call order (#143).

        A SEPARATE list from ``polled`` rather than a tuple inside it, because
        the two answer different questions and the older assertions read
        ``polled`` as a list of cursors. On Graph this is the whole defect
        surface: an omitted container is not a missing argument, it is a poll
        against ``me/mailFolders/inbox`` wearing the folder's cursor key.
        """

    def authenticate(self, interactive: bool = False) -> str:
        raise NotImplementedError

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        raise NotImplementedError

    def get_thread(self, thread_id: str) -> Thread:
        raise NotImplementedError

    def list_containers(self) -> list[Container]:
        return list(self.containers)

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.polled.append(cursor)
        self.polled_containers.append(container_id)
        return self.changes

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        if self.file_error is not None:
            raise self.file_error
        return folder

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        raise NotImplementedError

    def set_important(self, message_id: str, important: bool = True) -> str:
        raise AssertionError("the watcher must not flag messages")

    def tag_message(self, message_id: str, label: str) -> str:
        raise AssertionError("the watcher must file messages out of the inbox")

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        raise AssertionError("the watcher must never create a draft")

    def create_self_draft(self, subject: str, body: str) -> str:
        raise AssertionError("the watcher must never create a draft")


class CountingClassifier:
    """A deterministic classifier that records every message it was shown.

    ``seen_ids`` is the assertion surface for the scope-before-classify rule:
    an id in this list is an id whose subject, sender, and snippet were read.
    """

    def __init__(self, folder: str = FOLDER, confidence: float = 0.95) -> None:
        self.folder = folder
        self.confidence = confidence
        self.seen_ids: list[str] = []

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        self.seen_ids.append(message.id)
        return Classification(
            folder=self.folder,
            confidence=self.confidence,
            reason="fixed test decision",
            deterministic=False,
        )


def _message(message_id: str, *, containers: tuple[str, ...] = (INBOX_ID,)) -> Message:
    """A header projection that no rule in ``classify.RULES`` settles.

    Deliberately unremarkable -- a real sender, an ordinary subject, not
    flagged important, no Gmail category label -- so every rule falls through
    and the injected classifier is the layer that decides. A message the rule
    table settles would never reach the classifier, and these tests would then
    be asserting nothing about injection.
    """
    return Message(
        id=message_id,
        thread_id=f"t-{message_id}",
        sender="someone@example.com",
        subject="Quarterly review notes",
        received="2026-08-18T10:00:00Z",
        snippet="attached",
        containers=containers,
    )


def _context(
    provider: FakeProvider,
    *,
    scope: MailboxScope | None = None,
    state: WatchState | None = None,
    classifier: CountingClassifier | None = None,
    taxonomy: Taxonomy | None = None,
) -> watcher.RoundContext:
    return watcher.RoundContext(
        provider=provider,
        scope=scope if scope is not None else MailboxScope(mode="all_folders"),
        taxonomy=taxonomy if taxonomy is not None else default_taxonomy(),
        state=state if state is not None else WatchState(),
        container_id=None,
        classifier=classifier,
    )


class _FiledIdProvider(FakeProvider):
    """A mailbox whose filing reassigns the id, the way Graph's /move does.

    The returned object is a ``str`` subclass carrying the post-move id --
    the same shape ``providers.outlook.provider.FiledMessage`` returns,
    reproduced here rather than imported so these tests pin the INTERFACE
    contract (any adapter may reassign an id) instead of one adapter's class.

    ``destination_id`` and ``filed_as_prefix`` reproduce the two facts a real
    Graph mailbox carries and a bare name match cannot survive: the adapter
    knows the container id it moved INTO, and it files into ``prefix + folder``
    rather than the raw taxonomy name. Both default to off, so every fake built
    the old way still exercises the name-match fallback.
    """

    def __init__(
        self,
        changes: ChangeSet,
        *,
        containers: tuple[Container, ...] = (),
        file_error: Exception | None = None,
        destination_id: str | None = None,
        filed_as_prefix: str = "",
    ) -> None:
        super().__init__(changes, containers=containers, file_error=file_error)
        self.destination_id = destination_id
        self.filed_as_prefix = filed_as_prefix

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        if self.file_error is not None:
            raise self.file_error
        return _ReassignedId(
            f"{self.filed_as_prefix}{folder}",
            message_id=f"{message_id}-moved",
            destination_id=self.destination_id,
        )


class _ReassignedId(str):
    """``file_message`` result that reports the id the write actually created.

    ``destination_id`` is the container the write moved INTO. It is the fact
    that makes the destination replay mark resolvable without guessing at a
    display name, which no name match can do once a folder prefix is in play.
    """

    message_id: str | None
    destination_id: str | None

    def __new__(
        cls,
        filed_as: str,
        *,
        message_id: str | None,
        destination_id: str | None = None,
    ) -> _ReassignedId:
        value = super().__new__(cls, filed_as)
        value.message_id = message_id
        value.destination_id = destination_id
        return value
