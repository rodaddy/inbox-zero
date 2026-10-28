"""``list_containers`` on both adapters, and the vocabulary they must share.

The point of these tests is the WORD "same". ``Container.well_known`` promises
that "inbox" from Graph and "inbox" from Gmail mean the same thing, and that
promise is what lets shared code read placement without asking which provider
answered. A test per adapter would let the two vocabularies drift by one string
with nothing to say so, which is why the cross-provider assertions below run
against BOTH adapters in one test rather than twice in two.

No network and no credential: the Graph side uses a mocked httpx transport and
the Gmail side a fake Google client, matching the doubles in
``test_outlook_provider`` and ``test_gmail_provider``. Both are declared HERE
rather than imported from those modules because ``tests/`` is not a package;
each double is also narrowed to the folder/label shape this feature reads.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from inbox_zero_connector.providers.containers import MAX_FOLDER_DEPTH
from inbox_zero_connector.providers.gmail.provider import GmailProvider
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider

# Not a credential. Every test here runs against a mocked transport, so this
# string is never sent anywhere and authenticates nothing -- it satisfies the
# adapter's "do I already have a token" branch without touching the keychain.
# Same value and same reasoning as test_outlook_provider's constant; declared
# locally because tests/ is not a package and cross-importing a sibling test
# module would make one file's fixtures load-bearing for another's collection.
MOCK_TRANSPORT_SENTINEL = "mock-transport-only-authenticates-nothing"

GRAPH_ORIGIN = GRAPH_BASE.rsplit("/v1.0", 1)[0]
"""Scheme+host of ``GRAPH_BASE``, without its ``/v1.0`` segment.

A continuation link Graph issues is ABSOLUTE and includes ``/v1.0`` exactly
once. ``request.url.path`` in the double below also carries it, so the two are
composed against the origin rather than the base.
"""


class _Call:
    """Replays a canned response, like the google client's request object."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class FakeLabels:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def list(self, **kwargs: Any) -> _Call:
        self._log.append(("labels.list", kwargs))
        return _Call({"labels": self._store["labels"]})

    def create(self, **kwargs: Any) -> _Call:
        """Present so a CREATE can be OBSERVED, not so it can be used.

        ``test_gmail_list_containers_creates_no_label`` asserts this is never
        reached. A fake without a create method would fail with AttributeError
        instead, which proves the call was impossible rather than unmade.
        """
        self._log.append(("labels.create", kwargs))
        return _Call({"id": "LBL_NEW", "name": kwargs["body"]["name"]})


class FakeUsers:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def labels(self) -> FakeLabels:
        return FakeLabels(self._log, self._store)


class FakeService:
    """The minimum Gmail client surface ``list_containers`` touches."""

    def __init__(self, store: dict[str, Any]) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self._store = store

    def users(self) -> FakeUsers:
        return FakeUsers(self.log, self._store)


# ---------------------------------------------------------------------------
# Graph double: folders that NEST, and well-known aliases resolved by id
# ---------------------------------------------------------------------------


WELL_KNOWN_FOLDER_IDS = {
    "inbox": "F_INBOX",
    "junkemail": "F_JUNK",
    "deleteditems": "F_DELETED",
    "archive": "F_ARCHIVE",
    "sentitems": "F_SENT",
}
"""What each Graph alias resolves to. The ids are what ``well_known`` matches on.

Deliberately unrelated to the display names below: ``F_JUNK`` is reported with
the display name "Ordner", so a test that passed by matching "Junk" would be
matching a name this mailbox never uses.
"""


class FolderTree:
    """Mock Graph transport serving a NESTED folder tree.

    Graph's ``GET /me/mailFolders`` returns root-level folders only, and a child
    is reachable only through its parent's ``childFolders`` collection. This
    double reproduces that shape exactly -- serving children from the root
    listing would make the traversal test vacuous, because an adapter that never
    walked would still pass.
    """

    def __init__(self, children: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.root: list[dict[str, Any]] = [
            {"id": "F_INBOX", "displayName": "Posteingang"},
            {"id": "F_JUNK", "displayName": "Ordner"},
            {"id": "F_DELETED", "displayName": "Geloeschte Elemente"},
            {"id": "F_ARCHIVE", "displayName": "Archiv"},
            {"id": "F_SENT", "displayName": "Gesendete Elemente"},
            {"id": "F_CLIENTS", "displayName": "Clients"},
            {"id": "F_JUNK_DECOY", "displayName": "Junk"},
        ]
        """A localised mailbox with a DECOY.

        ``F_JUNK_DECOY`` is a user-created folder literally called "Junk" that
        is NOT the junk folder, and the real junk folder is called "Ordner".
        Any adapter resolving well-known by display name gets both wrong.
        """

        self.children: dict[str, list[dict[str, Any]]] = (
            {"F_CLIENTS": [{"id": "F_ACME", "displayName": "Acme"}]}
            if children is None
            else children
        )
        self.pages: dict[str, list[dict[str, Any]]] = {}
        """Second-and-later PAGES, keyed by the nextLink path the prior page carried.

        Graph splits a folder listing across pages exactly as it splits a
        message listing, and a walk that reads only the first page reports a
        mailbox that is missing folders with no error to say so. Keyed on the
        REQUEST PATH rather than an index so a walk that rewrote the link
        instead of replaying it verbatim misses the entry and 404s.
        """
        self.paths: list[str] = []
        self.urls: list[str] = []
        """Full request urls, query included, so a REPLAY can be checked verbatim."""
        self.missing_aliases: set[str] = set()
        """Aliases this mailbox does NOT have. Graph 404s them."""

    def _page(self, path: str, value: list[dict[str, Any]]) -> dict[str, Any]:
        """Attach an ``@odata.nextLink`` when this listing has a follow-on page."""
        body: dict[str, Any] = {"value": value}
        follow_on = f"{path}?%24skiptoken=PAGE2"
        if follow_on in self.pages:
            # ``path`` already carries the ``/v1.0`` prefix that ``GRAPH_BASE``
            # ends with, so the link is built from the ORIGIN. Concatenating
            # the two produces ``/v1.0/v1.0/...`` and the replay 404s.
            body["@odata.nextLink"] = f"{GRAPH_ORIGIN}{follow_on}"
        return body

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        self.urls.append(str(request.url))
        # Keyed on path PLUS query: the skiptoken that distinguishes page 2
        # from page 1 lives in the query string, and `url.path` drops it.
        located = f"{path}?{request.url.query.decode()}" if request.url.query else path
        if located in self.pages:
            return httpx.Response(200, json={"value": self.pages[located]})
        if path.endswith("/childFolders"):
            parent = path.rsplit("/", 2)[-2]
            return httpx.Response(200, json=self._page(path, self.children.get(parent, [])))
        alias = path.rsplit("/", 1)[-1]
        if alias in WELL_KNOWN_FOLDER_IDS:
            if alias in self.missing_aliases:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            folder_id = WELL_KNOWN_FOLDER_IDS[alias]
            return httpx.Response(200, json={"id": folder_id, "displayName": alias})
        if path.endswith("/mailFolders"):
            return httpx.Response(200, json=self._page(path, self.root))
        return httpx.Response(404, json={"error": {"message": f"unmapped {path}"}})


@pytest.fixture
def tree() -> FolderTree:
    return FolderTree()


@pytest.fixture
def outlook(tree: FolderTree) -> OutlookProvider:
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(tree.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


# ---------------------------------------------------------------------------
# Gmail double
# ---------------------------------------------------------------------------

GMAIL_LABELS = [
    {"id": "INBOX", "name": "Posteingang", "type": "system"},
    {"id": "SPAM", "name": "Werbung", "type": "system"},
    {"id": "TRASH", "name": "Papierkorb", "type": "system"},
    {"id": "SENT", "name": "Gesendet", "type": "system"},
    {"id": "IMPORTANT", "name": "Wichtig", "type": "system"},
    {"id": "Label_7", "name": "Clients/Acme", "type": "user"},
    {"id": "Label_8", "name": "Junk", "type": "user"},
]
"""Localised system labels plus a user label whose NAME contains a slash.

``Label_7`` is what proves Gmail hierarchy is never inferred, and ``Label_8`` is
the same decoy the Graph tree carries: a user label called "Junk" that is not
the spam label.
"""


@pytest.fixture
def gmail() -> GmailProvider:
    return GmailProvider(service=FakeService({"labels": list(GMAIL_LABELS)}))


def _by_id(containers: list[Any]) -> dict[str, Any]:
    return {container.id: container for container in containers}


# ---------------------------------------------------------------------------
# exclusive: the structural difference the model exists for
# ---------------------------------------------------------------------------


def test_outlook_marks_every_container_exclusive(outlook: OutlookProvider) -> None:
    """A Graph message sits in exactly one folder, so every folder is exclusive."""
    containers = outlook.list_containers()

    assert containers, "the fixture serves folders; an empty result proves nothing"
    assert all(container.exclusive is True for container in containers)


def test_gmail_marks_every_container_non_exclusive(gmail: GmailProvider) -> None:
    """A Gmail message routinely carries several labels, so none is exclusive."""
    containers = gmail.list_containers()

    assert containers
    assert all(container.exclusive is False for container in containers)


def test_the_two_adapters_disagree_about_exclusivity_by_design(
    outlook: OutlookProvider, gmail: GmailProvider
) -> None:
    """Stated as one assertion so the difference cannot be read as an accident."""
    graph_flags = {container.exclusive for container in outlook.list_containers()}
    gmail_flags = {container.exclusive for container in gmail.list_containers()}

    assert graph_flags == {True}
    assert gmail_flags == {False}


# ---------------------------------------------------------------------------
# well_known: ONE vocabulary, resolved by id on both sides
# ---------------------------------------------------------------------------


def test_both_adapters_answer_in_the_same_well_known_vocabulary(
    outlook: OutlookProvider, gmail: GmailProvider
) -> None:
    """THE cross-provider assertion. Shared code must never branch on provider.

    Gmail is a SUBSET rather than an equal set, and only by the one documented
    difference: Gmail has no archive label, because archiving is the absence of
    INBOX. Asserting the subset relation plus the exact missing element pins
    that difference instead of letting any mismatch pass.
    """
    graph_tags = {c.well_known for c in outlook.list_containers() if c.well_known}
    gmail_tags = {c.well_known for c in gmail.list_containers() if c.well_known}

    assert graph_tags == {"inbox", "junk", "deleted", "archive", "sent"}
    assert gmail_tags < graph_tags
    assert graph_tags - gmail_tags == {"archive"}


def test_outlook_resolves_well_known_by_id_not_by_display_name(
    outlook: OutlookProvider,
) -> None:
    """The junk folder is called "Ordner"; a folder called "Junk" is not it.

    This is the test the whole Graph implementation is shaped around. The v1.0
    mailFolder resource has no wellKnownName property, so the adapter GETs each
    alias and keeps the id -- and a display-name match would tag the decoy and
    miss the real one.
    """
    containers = _by_id(outlook.list_containers())

    assert containers["F_JUNK"].name == "Ordner"
    assert containers["F_JUNK"].well_known == "junk"
    assert containers["F_JUNK_DECOY"].name == "Junk"
    assert containers["F_JUNK_DECOY"].well_known is None


def test_gmail_resolves_well_known_by_label_id_not_by_name(gmail: GmailProvider) -> None:
    """Gmail's system label NAMES are localised; the ids are not. Same rule."""
    containers = _by_id(gmail.list_containers())

    assert containers["SPAM"].name == "Werbung"
    assert containers["SPAM"].well_known == "junk"
    assert containers["Label_8"].name == "Junk"
    assert containers["Label_8"].well_known is None


def test_gmail_has_no_archive_container(gmail: GmailProvider) -> None:
    """Archiving on Gmail is the ABSENCE of INBOX, so there is no row to tag."""
    assert all(c.well_known != "archive" for c in gmail.list_containers())


def test_a_user_container_carries_no_well_known_tag(
    outlook: OutlookProvider, gmail: GmailProvider
) -> None:
    """None means "no provider-independent meaning", on both sides."""
    assert _by_id(outlook.list_containers())["F_CLIENTS"].well_known is None
    assert _by_id(gmail.list_containers())["Label_7"].well_known is None


def test_outlook_reports_no_tag_for_an_alias_this_mailbox_lacks(
    outlook: OutlookProvider, tree: FolderTree
) -> None:
    """Not every Exchange mailbox has an archive folder. A 404 is not a crash.

    The absence of a tag is the truthful answer; refusing the whole enumeration
    over one missing well-known folder would not be.
    """
    tree.missing_aliases = {"archive"}

    tags = {c.well_known for c in outlook.list_containers() if c.well_known}

    assert "archive" not in tags
    assert tags == {"inbox", "junk", "deleted", "sent"}


# ---------------------------------------------------------------------------
# parent_id: Graph nests, Gmail does not
# ---------------------------------------------------------------------------


def test_gmail_always_reports_a_null_parent(gmail: GmailProvider) -> None:
    """Including for "Clients/Acme", which LOOKS nested and is not.

    The Gmail Label resource carries no parent field, so the "/" is a display
    convention. Splitting on it would invent hierarchy the provider never
    stated and would misparse a user label that merely contains a slash.
    """
    containers = gmail.list_containers()

    assert all(container.parent_id is None for container in containers)
    assert _by_id(containers)["Label_7"].name == "Clients/Acme"


def test_outlook_traverses_into_child_folders(outlook: OutlookProvider, tree: FolderTree) -> None:
    """GET /me/mailFolders is ROOT-ONLY, so a nested folder proves a walk happened."""
    containers = _by_id(outlook.list_containers())

    assert "F_ACME" in containers, "the child folder was never fetched"
    assert containers["F_ACME"].parent_id == "F_CLIENTS"
    assert containers["F_CLIENTS"].parent_id is None
    assert any(path.endswith("/F_CLIENTS/childFolders") for path in tree.paths)


def test_outlook_stops_the_walk_at_the_documented_depth(tree: FolderTree) -> None:
    """A cycle-shaped tree must terminate, bounded by MAX_FOLDER_DEPTH.

    ``F_LOOP`` is its own child, which is not a shape Graph produces -- it is
    the cheapest way to prove the bound is what stops the recursion rather than
    the fixture running out of folders.
    """
    tree.root = [{"id": "F_LOOP", "displayName": "Loop"}]
    tree.children = {"F_LOOP": [{"id": "F_LOOP", "displayName": "Loop"}]}
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(tree.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    provider = OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)

    containers = provider.list_containers()

    # The LITERAL, not the constant. Asserting against MAX_FOLDER_DEPTH would
    # track any edit to it, so raising the bound to 99 would still pass -- the
    # test would prove the walk terminates without proving where.
    assert len(containers) == 6
    assert MAX_FOLDER_DEPTH == 6, "the documented bound changed; update this test deliberately"


# ---------------------------------------------------------------------------
# Read-only: neither adapter creates anything while enumerating
# ---------------------------------------------------------------------------


def test_outlook_list_containers_issues_no_write(
    outlook: OutlookProvider, tree: FolderTree
) -> None:
    """``_folder_id`` creates a missing folder; this path must never do that."""
    outlook.list_containers()

    assert tree.paths, "no request was made at all"


def test_gmail_list_containers_creates_no_label(gmail: GmailProvider) -> None:
    """``_label_id`` creates a missing label; this path must only list."""
    gmail.list_containers()
    service: FakeService = gmail._service  # type: ignore[assignment]

    called = {name for name, _ in service.log}
    assert called == {"labels.list"}


# ---------------------------------------------------------------------------
# Pagination: Graph splits a folder listing, and a walk must exhaust it
# ---------------------------------------------------------------------------


def _paged_provider(tree: FolderTree) -> OutlookProvider:
    """A provider over ``tree`` after its pages have been registered."""
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(tree.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


def test_outlook_follows_the_root_listing_to_its_last_page(tree: FolderTree) -> None:
    """A folder on page 2 of ``/me/mailFolders`` must still be reported.

    Graph pages a folder listing exactly as it pages messages. A walk that
    reads only the first page reports a mailbox that is MISSING folders, with
    no error and no log line to say so -- the caller cannot tell a short answer
    from a small mailbox.
    """
    tree.pages = {
        "/v1.0/me/mailFolders?%24skiptoken=PAGE2": [{"id": "F_P2", "displayName": "Page2"}]
    }

    containers = _by_id(_paged_provider(tree).list_containers())

    assert "F_P2" in containers, "the second page of the root listing was never fetched"
    assert containers["F_P2"].parent_id is None


def test_outlook_follows_a_child_listing_to_its_last_page(tree: FolderTree) -> None:
    """The same rule one level down: ``childFolders`` pages too."""
    tree.pages = {
        "/v1.0/me/mailFolders/F_CLIENTS/childFolders?%24skiptoken=PAGE2": [
            {"id": "F_BETA", "displayName": "Beta"}
        ]
    }

    containers = _by_id(_paged_provider(tree).list_containers())

    assert "F_BETA" in containers, "the second page of the child listing was never fetched"
    assert containers["F_BETA"].parent_id == "F_CLIENTS"


def test_outlook_replays_the_next_link_verbatim(tree: FolderTree) -> None:
    """The link is Graph's own opaque state, never parsed, edited, or rebuilt.

    Registered under the EXACT path the server issued, so a walk that
    reconstructed the url -- reordering, re-encoding, or re-adding ``$top`` --
    misses the entry and 404s rather than passing by accident.
    """
    tree.pages = {
        "/v1.0/me/mailFolders?%24skiptoken=PAGE2": [{"id": "F_P2", "displayName": "Page2"}]
    }

    _paged_provider(tree).list_containers()

    issued = f"{GRAPH_ORIGIN}/v1.0/me/mailFolders?%24skiptoken=PAGE2"
    assert issued in tree.urls, (
        "the continuation was not replayed byte-for-byte; the walk rebuilt the url "
        f"(observed: {[u for u in tree.urls if 'PAGE2' in u]})"
    )
