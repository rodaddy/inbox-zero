"""Label naming, the change feed, and what the granted OAuth scopes really were.

Split out of ``test_gmail_provider.py`` on 2026-08-19 (500-code-line ceiling).
Three concerns that share no fixtures with the draft or query-injection paths:

* The label-name prefix -- the same operator setting as Graph's folder prefix,
  asserted to render a taxonomy folder to the SAME name on both providers, and
  to match an existing label rather than creating a second one beside it.
* ``poll_changes`` -- the watcher's change feed, READ ONLY. Cold start must not
  walk history it has no position for, a refused position is a resync rather
  than an error, and no cursor is stored while pages remain, because storing one
  early silently drops every change on the pages not yet read.
* Granted-scope discovery -- the gap between a credential's ``scopes`` (what we
  REQUESTED) and ``granted_scopes`` (what Google ANSWERED). Reading the wrong
  attribute produces a confident, plausible, entirely wrong report, so the
  restored-credential case is named explicitly rather than left to a sweep.
"""

from __future__ import annotations

from typing import Any

import pytest

from _gmail_harness import (
    LEGACY_LABEL_PREFIX,
    FakeService,
    GmailProvider,
    _message,
    gmail,
)
from _gmail_harness import (
    drafting_provider as drafting_provider,
)
from _gmail_harness import (
    provider as provider,
)
from _gmail_harness import (
    store as store,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.gmail import history as gmail_history

# ---------------------------------------------------------------------------
# Label-name prefix. Same setting as Graph's folder prefix and the same
# default (empty), so the two providers cannot disagree about what the
# taxonomy folder ``Reference`` is called in the owner's mailbox.
# ---------------------------------------------------------------------------


def test_the_default_prefix_creates_a_label_named_exactly_the_taxonomy_folder(
    store: dict[str, Any],
) -> None:
    """``Reference`` must reach Gmail as ``Reference``, not ``InboxZero/Reference``.

    Asserted on the CREATE payload as well as the receipt: the payload is the
    label the owner sees, and a test that checked only the return value would
    pass against an adapter that named the two differently.
    """
    provider = GmailProvider(service=FakeService(store))
    assert provider.file_message("m1", "Reference") == "Reference"
    service: FakeService = provider._service  # type: ignore[assignment]
    created = [kwargs for name, kwargs in service.log if name == "labels.create"]
    assert created[0]["body"]["name"] == "Reference"


def test_an_operator_prefix_restores_the_previous_label_names(store: dict[str, Any]) -> None:
    """The old nesting is still reachable, byte-for-byte.

    The trailing slash is what makes Gmail render these as children of one
    parent label, which is why the prefix is a free string rather than a
    boolean: restoring the old behaviour and choosing a flat prefix are
    different requests.
    """
    provider = GmailProvider(service=FakeService(store), folder_prefix=LEGACY_LABEL_PREFIX)
    assert provider.file_message("m1", "Reference") == "InboxZero/Reference"
    service: FakeService = provider._service  # type: ignore[assignment]
    created = [kwargs for name, kwargs in service.log if name == "labels.create"]
    assert created[0]["body"]["name"] == "InboxZero/Reference"


def test_the_prefix_matches_an_existing_label_rather_than_creating_a_second_one(
    store: dict[str, Any],
) -> None:
    """A prefixed deployment must RESOLVE its old label, not duplicate it."""
    store["labels"] = [{"id": "LBL_LEGACY", "name": "InboxZero/Reference"}]
    provider = GmailProvider(service=FakeService(store), folder_prefix=LEGACY_LABEL_PREFIX)
    provider.file_message("m1", "Reference")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert not any(name == "labels.create" for name, _ in service.log)
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert modify["body"]["addLabelIds"] == ["LBL_LEGACY"]


# ---------------------------------------------------------------------------
# Case. Gmail label-name uniqueness IS case-insensitive, so a case variant is
# THE SAME LABEL -- and the create-once fallback must never fire for one.
# ---------------------------------------------------------------------------


def test_an_existing_label_matches_regardless_of_case(store: dict[str, Any]) -> None:
    """``reference`` in the mailbox IS the label ``Reference`` resolves to.

    Gmail refuses to mint a second label whose name differs only in case, so a
    case-sensitive match does not merely miss -- it drives the create-once
    fallback into a guaranteed 409 on every single filing.
    """
    store["labels"] = [{"id": "LBL_LOWER", "name": "reference"}]
    provider = GmailProvider(service=FakeService(store))

    provider.file_message("m1", "Reference")

    service: FakeService = provider._service  # type: ignore[assignment]
    assert not any(name == "labels.create" for name, _ in service.log), (
        "a case variant is the same label; creating one is the defect"
    )
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert modify["body"]["addLabelIds"] == ["LBL_LOWER"]


def test_a_case_variant_is_matched_in_either_direction(store: dict[str, Any]) -> None:
    """Upper-cased in the mailbox resolves too -- the rule is symmetric."""
    store["labels"] = [{"id": "LBL_UPPER", "name": "REFERENCE"}]
    provider = GmailProvider(service=FakeService(store))

    provider.file_message("m1", "Reference")

    service: FakeService = provider._service  # type: ignore[assignment]
    assert not any(name == "labels.create" for name, _ in service.log)
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert modify["body"]["addLabelIds"] == ["LBL_UPPER"]


def test_an_exact_match_wins_over_a_case_variant(store: dict[str, Any]) -> None:
    """Case-insensitive matching must not make the resolution AMBIGUOUS.

    Gmail cannot actually hold both rows, so this is defensive: if a listing
    ever carries both, the exactly-named one is the answer rather than
    whichever the iteration order reached first.
    """
    store["labels"] = [
        {"id": "LBL_LOWER", "name": "reference"},
        {"id": "LBL_EXACT", "name": "Reference"},
    ]
    provider = GmailProvider(service=FakeService(store))

    provider.file_message("m1", "Reference")

    service: FakeService = provider._service  # type: ignore[assignment]
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert modify["body"]["addLabelIds"] == ["LBL_EXACT"]


def test_a_409_on_create_is_resolved_by_re_listing_once(store: dict[str, Any]) -> None:
    """The race: another client minted the label between our list and our create.

    Gmail answers ``409 Conflict``. Raising there fails a filing over a label
    that DOES exist, so the adapter re-lists once and uses the row that is now
    there. Bounded to ONE re-list: a 409 that survives it is a real refusal.
    """
    from googleapiclient.errors import HttpError

    store["labels"] = []

    class _Resp:
        status = 409
        reason = "Conflict"

    def _conflict(**kwargs: Any) -> Any:
        # The label appears in the mailbox exactly as a competing client would
        # have left it -- and under a DIFFERENT case, so this also proves the
        # recovery re-list uses the case-insensitive match.
        store["labels"].append({"id": "LBL_RACED", "name": "reference"})
        raise HttpError(_Resp(), b"label exists")

    store["label_create_hook"] = _conflict
    service = FakeService(store)
    provider = GmailProvider(service=service)

    provider.file_message("m1", "Reference")

    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert modify["body"]["addLabelIds"] == ["LBL_RACED"]
    assert sum(1 for name, _ in service.log if name == "labels.create") == 1, (
        "the create must be attempted once, not retried in a loop"
    )


def test_the_prefix_comes_from_the_keystone_when_the_adapter_is_given_none(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No constructor override means the SETTING decides, not a hardcoded string.

    ``None`` and ``""`` are different answers -- empty is the default prefix and
    None means "ask config" -- so this proves the sentinel is not collapsing
    them, which a plain falsy check would.
    """
    from inbox_zero_connector.config import ENV_PREFIX, reset_settings

    monkeypatch.setenv(f"{ENV_PREFIX}FOLDER_PREFIX", "Filed/")
    reset_settings()
    provider = GmailProvider(service=FakeService(store))
    assert provider.file_message("m1", "Reference") == "Filed/Reference"
    service: FakeService = provider._service  # type: ignore[assignment]
    created = [kwargs for name, kwargs in service.log if name == "labels.create"]
    assert created[0]["body"]["name"] == "Filed/Reference"


def test_both_providers_render_the_same_folder_the_same_way(store: dict[str, Any]) -> None:
    """The point of ONE setting: no mailbox is prefixed on one provider and bare
    on the other. Rendering is compared directly rather than through two live
    fixtures, because what must agree is the string-building rule -- Graph's
    ``_folder_name`` and Gmail's ``_label_name`` are the same function twice.
    """
    from inbox_zero_connector.providers.outlook.provider import OutlookProvider

    for prefix in ("", "InboxZero-", "Filed/"):
        gmail = GmailProvider(service=FakeService(store), folder_prefix=prefix)
        outlook = OutlookProvider(folder_prefix=prefix)
        assert gmail._label_name("Reference") == outlook._folder_name("Reference")


def test_a_gmail_message_carries_every_place_label_as_a_container(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """Gmail's answer is genuinely many PLACES, so all of THOSE survive.

    ``Container.exclusive`` is False for Gmail: a message routinely holds
    several labels at once. Collapsing them would force the adapter to pick
    one, and the adapter has no basis on which to pick.

    ``UNREAD`` and ``IMPORTANT`` are NOT among them. They are message FLAGS,
    and the model already carries each as its own field -- asserted here in
    the same test, because "filtered out of containers" and "silently lost"
    look identical from a containers assertion alone.
    """
    store["messages"]["m1"] = _message(
        "m1", "First", labels=["INBOX", "UNREAD", "Label_Legal", "IMPORTANT"]
    )
    messages = provider.list_inbox(limit=10)
    assert messages[0].containers == ("INBOX", "Label_Legal")
    assert messages[0].is_unread is True
    assert messages[0].is_important is True


def test_gmail_flag_and_category_labels_are_never_containers(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """Every flag Gmail ships at once, and the inbox is the only PLACE left.

    THE LIVE DEFECT THIS PINS (head-verified against a real mailbox
    2026-08-22). ``MailboxScope.covers_all`` is any-exclusion-wins, so under
    the default ``inbox_only`` mode a single uncovered entry refuses the whole
    message. Reporting ``UNREAD`` as a container therefore made the watcher
    refuse EVERY unread inbox message -- which is every message it exists to
    read. The ``CATEGORY_*`` tab labels fail the same way for the same reason.
    """
    store["messages"]["m1"] = _message(
        "m1",
        "First",
        labels=[
            "INBOX",
            "UNREAD",
            "STARRED",
            "IMPORTANT",
            "CATEGORY_PERSONAL",
            "CATEGORY_UPDATES",
        ],
    )
    messages = provider.list_inbox(limit=10)
    assert messages[0].containers == ("INBOX",)


def test_gmail_category_labels_are_reported_as_categories(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """Filtered out of ``containers``, but NOT lost -- they land in ``categories``.

    A category is an ATTRIBUTE of the message the way ``is_unread`` is, not a
    second place it sits, so the model carries it as its own field. Asserted
    alongside ``containers`` in one test because "moved to the right field" and
    "dropped on the floor" are indistinguishable from a containers assertion
    alone -- and dropping it is what killed the ``CATEGORY_PROMOTIONS``
    triage rule when the flag filter first landed.
    """
    store["messages"]["m1"] = _message(
        "m1", "First", labels=["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"]
    )
    messages = provider.list_inbox(limit=10)
    assert messages[0].containers == ("INBOX",)
    assert messages[0].categories == ("CATEGORY_PROMOTIONS",)


def test_a_gmail_message_with_no_category_label_carries_no_categories(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """No tab label means an EMPTY tuple, never an invented default.

    Gmail applies a category only when its own model has one to state, and
    Outlook has no equivalent concept at all -- so ``()`` is the ordinary
    answer and every reader of the field must handle it.
    """
    store["messages"]["m1"] = _message("m1", "First", labels=["INBOX", "Label_Legal"])
    messages = provider.list_inbox(limit=10)
    assert messages[0].categories == ()


def test_a_gmail_message_with_no_labels_carries_no_containers(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """An unlabelled message reports an EMPTY tuple, not a placeholder.

    Callers that need placement must handle the empty case; inventing one here
    would hand them a container the provider never named.
    """
    # Built off _message and then emptied, because the helper's `labels or
    # [...]` default cannot express "no labels" -- an empty list falls through
    # to the INBOX/UNREAD default, which is the case under test.
    unlabelled = _message("m1", "First")
    unlabelled["labelIds"] = []
    store["messages"]["m1"] = unlabelled
    messages = provider.list_inbox(limit=10)
    assert messages[0].containers == ()


# ---------------------------------------------------------------------------
# poll_changes -- the watcher's change feed. READ ONLY.
# ---------------------------------------------------------------------------


def _http_error(status: int) -> Any:
    """A googleapiclient HttpError carrying ``status``, built without a network.

    The SDK reads the code off ``resp.status``, so a stand-in response object
    with that attribute is all the adapter's check can see -- and building it
    here keeps the test honest about WHAT is being asserted: the status, not a
    message string that could be reworded upstream.
    """
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 0
        reason = "test"

    resp = _Resp()
    resp.status = status
    return HttpError(resp, b'{"error": {"code": %d}}' % status)


def test_poll_changes_cold_start_returns_a_cursor_without_walking_history(
    provider: GmailProvider,
) -> None:
    """``cursor=None`` has no position to walk FROM, so it must not try.

    Calling the history feed with no ``startHistoryId`` would be asking Gmail
    to walk from nowhere. The cold start reads the mailbox's CURRENT position
    instead, which is what the next round resumes from.
    """
    changes = provider.poll_changes(None)

    assert changes.cursor == "9001"
    assert changes.resync_required is False
    assert changes.has_more is False
    assert [m.id for m in changes.messages] == ["m1", "m2"]

    service: FakeService = provider._service  # type: ignore[assignment]
    called = [name for name, _ in service.log]
    assert "history.list" not in called, "a cold start must not walk history"
    assert "getProfile" in called


def test_poll_changes_returns_changed_messages_and_a_new_cursor(
    provider: GmailProvider,
) -> None:
    """The ordinary round: replay the token, get what changed and where to resume."""
    changes = provider.poll_changes("9001")

    assert [m.id for m in changes.messages] == ["m2"]
    assert changes.cursor == "9100"
    assert changes.resync_required is False
    assert changes.has_more is False

    service: FakeService = provider._service  # type: ignore[assignment]
    requests = [kwargs for name, kwargs in service.log if name == "history.list"]
    # The token is replayed VERBATIM -- not parsed, incremented, or normalised.
    assert requests[0]["startHistoryId"] == "9001"
    # Only messageAdded is consumed; the other record types are read-side noise
    # for a watcher whose entire output is an added label.
    assert requests[0]["historyTypes"] == ["messageAdded"]


def test_poll_changes_treats_a_refused_position_as_resync_not_an_error(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """Gmail refusing an aged-out position is NORMAL OPERATION, not a failure.

    Every provider evicts resume positions eventually, so a watcher that raised
    here would raise on a perfectly healthy quiet mailbox. The adapter answers
    with the flag and lets the caller cold start.
    """
    store["history_error"] = _http_error(404)

    changes = provider.poll_changes("1")

    assert changes.resync_required is True
    assert changes.cursor is None, "a resync must not hand back a storable cursor"
    assert changes.messages == ()


def test_poll_changes_raises_on_a_failure_that_is_not_a_refused_position(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """The complement, so "404 is not an error" is not read as "nothing is".

    Without this, an adapter that swallowed every HttpError would pass the test
    above. A 400 is a genuine failure and must surface.
    """

    store["history_error"] = _http_error(400)

    with pytest.raises(ProviderError, match="poll_changes failed"):
        provider.poll_changes("9001")


def test_poll_changes_returns_no_storable_cursor_while_pages_remain(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """THE CURSOR RULE (WATCHER-SPEC.md 2.4), asserted at the adapter boundary.

    Storing a cursor mid-page marks a partial read as a completed one, and
    every change on the pages never fetched is lost with no error, no
    exception, and no log line. The unfinished page therefore reports its
    continuation handle and NO cursor.
    """
    store["history"] = {
        "historyId": "9100",
        "nextPageToken": "PAGE_2",
        "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}],
    }

    changes = provider.poll_changes("9001")

    assert changes.has_more is True
    # S105/S106 match the NAME "token", not the value. A Gmail page token is an
    # opaque pagination handle from a response body -- it grants nothing,
    # authenticates nothing, and dies with the query. Ignored per line rather
    # than per file so a real hardcoded credential here is still caught.
    assert changes.next_page_token == "PAGE_2"  # ruff: ignore[hardcoded-password-string]
    assert changes.cursor is None, "an unfinished page must not hand back a cursor"
    assert [m.id for m in changes.messages] == ["m2"]


def test_a_change_set_cannot_be_built_with_a_cursor_while_pages_remain() -> None:
    """The rule is enforced by the TYPE, not only by the adapter that obeys it.

    A flag a caller must remember to consult is the failure mode this model
    exists to remove: the mid-page cursor is unrepresentable, so an adapter bug
    that would have caused silent permanent mail loss is a loud
    ValidationError naming the field instead.
    """
    from pydantic import ValidationError

    from inbox_zero_connector.providers.base import ChangeSet

    with pytest.raises(ValidationError, match="pages remaining"):
        ChangeSet(cursor="9100", next_page_token="PAGE_2")  # ruff: ignore[hardcoded-password-func-arg]

    with pytest.raises(ValidationError, match="resync"):
        ChangeSet(cursor="9100", resync_required=True)


def test_poll_changes_continues_from_a_page_token(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """The continuation handle goes back to the adapter and reaches the request.

    Without this the paging flag would be observable and unusable -- the loop
    could see that pages remain and have no way to fetch them.
    """
    provider.poll_changes("9001", page_token="PAGE_2")  # ruff: ignore[hardcoded-password-func-arg]

    service: FakeService = provider._service  # type: ignore[assignment]
    requests = [kwargs for name, kwargs in service.log if name == "history.list"]
    assert requests[0]["pageToken"] == "PAGE_2"
    assert requests[0]["startHistoryId"] == "9001"


def test_poll_changes_reports_a_repeated_message_once_per_round(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """One message named by several records is ONE message to process.

    A message gaining several labels in one round produces several
    ``messagesAdded`` records. Returning it twice would make the loop fetch and
    process it twice within a SINGLE round, which the seen-ids guard cannot
    prevent because nothing has been marked seen yet.
    """
    store["history"] = {
        "historyId": "9100",
        "history": [
            {"messagesAdded": [{"message": {"id": "m2"}}]},
            {"messagesAdded": [{"message": {"id": "m2"}}, {"message": {"id": "m1"}}]},
        ],
    }

    changes = provider.poll_changes("9001")

    assert [m.id for m in changes.messages] == ["m2", "m1"]


def test_poll_changes_bounds_the_history_page_it_asks_for(
    provider: GmailProvider,
) -> None:
    """An unbounded ``history.list`` lets noise burn the round's page budget.

    Gmail's history feed is MAILBOX-WIDE: every SENT and DRAFT record shares the
    page with the inbox arrivals this watcher acts on. Without ``maxResults``
    Gmail picks the page size, so a busy sending day can fill a page with
    records the adapter discards and push the arrivals onto a page the round's
    budget never reaches -- read as a quiet mailbox, with no error.

    ``labelId`` is NOT asserted here, and that absence is the point: the
    parameter accepts one label, and this stream has no single-label identity to
    give it (``GmailProvider.poll_changes`` ignores ``container_id`` and stores
    one mailbox-wide cursor). Asserting one would bake a read scope the adapter
    does not own.
    """
    provider.poll_changes("9001")

    service: FakeService = provider._service  # type: ignore[assignment]
    requests = [kwargs for name, kwargs in service.log if name == "history.list"]
    assert requests[0]["maxResults"] == gmail_history.HISTORY_PAGE_LIMIT
    assert "labelId" not in requests[0], "the stream is mailbox-wide; it has no one label"


def test_poll_changes_does_not_restore_the_supplied_cursor_on_a_final_page(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """A final page with no ``historyId`` must not replay the AGED position.

    Re-storing the cursor that was just walked from leaves a quiet mailbox
    pinned to the same ageing token every round, until Gmail evicts it and the
    watcher takes a full resync it never needed. ``None`` is the honest answer:
    nothing new to store, and the caller keeps what it already had.
    """
    store["history"] = {"history": []}

    changes = provider.poll_changes("9001")

    assert changes.cursor is None, "an absent historyId must not re-store the input"
    assert changes.has_more is False
    assert changes.messages == ()


def test_poll_changes_stores_the_response_position_on_a_final_page(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """The complement, so "None on a final page" is not read as "never a cursor".

    When Gmail DOES report a top-level ``historyId``, that is the new position
    and it is what gets stored -- the ordinary advance the watcher lives on.
    """
    store["history"] = {"historyId": "9200", "history": []}

    changes = provider.poll_changes("9001")

    assert changes.cursor == "9200"


def test_the_history_module_logs_under_the_same_provider_name() -> None:
    """The duplicated literal cannot drift from the one the adapter uses.

    ``gmail_history`` restates the provider name instead of importing it,
    because ``gmail`` imports ``gmail_history`` and taking the name back the
    other way would be a circular import. A restatement is only safe if
    something fails when the two disagree -- this is that something.
    """
    from inbox_zero_connector.providers.gmail import history as gmail_history
    from inbox_zero_connector.providers.gmail import provider as gmail

    assert gmail_history.PROVIDER_LOG_NAME == gmail.PROVIDER


def test_the_query_module_and_the_adapter_agree_on_the_sent_label() -> None:
    """Same rule for the SENT label moved alongside the sent-sample guard."""
    from inbox_zero_connector.providers.gmail import provider as gmail
    from inbox_zero_connector.providers.gmail import query as gmail_query

    assert gmail_query.SENT_LABEL == gmail.SENT_LABEL


# ---------------------------------------------------------------------------
# Granted-scope discovery: the ANSWER, never the ask
#
# The whole point of these tests is the gap between two adjacent attributes on
# a google-auth credential -- `scopes` (what we REQUESTED) and `granted_scopes`
# (what Google ANSWERED). Reading the wrong one produces a confident, plausible,
# entirely wrong report, so the restored-credential case below is named
# explicitly rather than left implicit in a coverage sweep.
# ---------------------------------------------------------------------------

REQUESTED = "https://www.googleapis.com/auth/gmail.modify"
GRANTED_NARROWER = "https://www.googleapis.com/auth/gmail.readonly"


class _FakeCreds:
    """A credential carrying the request and the answer separately, as google-auth does."""

    def __init__(self, scopes: Any, granted_scopes: Any) -> None:
        self.scopes = scopes
        self.granted_scopes = granted_scopes
        # S105 matches the NAMES here, not the values. These are one-character
        # placeholders on a fake object that never reaches an auth server;
        # `granted_scopes` is the only attribute any assertion reads. Ignored
        # per line, per the pattern above, so a real credential is still caught.
        self.token = "t"  # ruff: ignore[hardcoded-password-string]
        self.refresh_token = "r"  # ruff: ignore[hardcoded-password-string]
        self.token_uri = "u"  # ruff: ignore[hardcoded-password-string]
        self.client_id = "c"
        self.client_secret = "s"  # ruff: ignore[hardcoded-password-string]


def _provider_authenticated_with(
    monkeypatch: pytest.MonkeyPatch, creds: Any, service: Any
) -> GmailProvider:
    """Run authenticate() with a canned credential and a canned service."""
    provider = GmailProvider()
    monkeypatch.setattr(GmailProvider, "_credentials", lambda self, interactive: creds)
    monkeypatch.setattr(gmail, "build", lambda *a, **k: service)
    provider.authenticate(interactive=False)
    return provider


def test_gmail_retains_the_granted_set_from_a_fresh_authentication(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]
) -> None:
    """A fresh consent carries granted_scopes, and that is what is reported."""
    creds = _FakeCreds(scopes=[REQUESTED], granted_scopes=[GRANTED_NARROWER])
    provider = _provider_authenticated_with(monkeypatch, creds, FakeService(store))
    assert provider.granted_scopes() == (GRANTED_NARROWER,)


def test_gmail_reports_the_narrower_grant_not_the_wider_request(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]
) -> None:
    """A user un-ticking a permission must shrink the report, not be overridden."""
    creds = _FakeCreds(scopes=[REQUESTED], granted_scopes=[GRANTED_NARROWER])
    provider = _provider_authenticated_with(monkeypatch, creds, FakeService(store))
    granted = provider.granted_scopes()
    assert granted is not None
    assert REQUESTED not in granted


def test_a_restored_credential_never_reports_the_requested_list_as_the_grant(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]
) -> None:
    """THE TRAP. A keychain-restored credential must report not-discoverable.

    ``_credentials`` restores with
    ``Credentials.from_authorized_user_info(payload, list(self.scopes))``.
    google-auth assigns that REQUESTED list to ``.scopes`` and leaves
    ``.granted_scopes`` as None (``credentials.py:470-515``), so a naive read of
    ``.scopes`` here would hand back our own ask dressed as the tenant's answer
    -- the exact failure grant discovery exists to prevent.

    None (not-discoverable) is the required report. ``grants`` fails closed on
    it AND flags it as unreadable, which is loud; reporting the requested list
    would be silent and wrong.
    """
    restored = _FakeCreds(scopes=[REQUESTED], granted_scopes=None)
    provider = _provider_authenticated_with(monkeypatch, restored, FakeService(store))
    assert provider.granted_scopes() is None, (
        "a restored credential reported its REQUESTED scopes as the grant"
    )


def test_a_restored_credential_is_not_reported_as_an_empty_grant(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]
) -> None:
    """Undetermined must stay distinguishable from a genuinely empty grant."""
    restored = _FakeCreds(scopes=[REQUESTED], granted_scopes=None)
    provider = _provider_authenticated_with(monkeypatch, restored, FakeService(store))
    assert provider.granted_scopes() != ()


def test_an_unauthenticated_gmail_adapter_reports_not_discoverable() -> None:
    """An injected-service adapter has seen no token response, so it knows nothing."""
    assert GmailProvider(service=object()).granted_scopes() is None


def test_gmail_returns_scope_uris_raw_without_normalizing_them(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]
) -> None:
    """Interpretation belongs to grants, so the full URI survives untouched."""
    creds = _FakeCreds(scopes=[REQUESTED], granted_scopes=[GRANTED_NARROWER])
    provider = _provider_authenticated_with(monkeypatch, creds, FakeService(store))
    assert provider.granted_scopes() == (GRANTED_NARROWER,)


def test_gmail_satisfies_the_scope_source_protocol_structurally() -> None:
    from inbox_zero_connector.services.auth.grants import ScopeSource

    assert isinstance(GmailProvider(service=object()), ScopeSource)
