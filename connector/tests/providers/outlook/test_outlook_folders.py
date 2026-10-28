"""Folder resolution: the operator prefix, missing ids, and the container model.

Split out of ``test_outlook_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns the banners about naming and locating a mailbox folder:

* Round 6 sweep -- a folder with no id refuses rather than filing nowhere.
* Folder-name prefix -- live-verified 2026-08-18 against Exchange Online, where
  the hardcoded ``InboxZero-`` gave the owner a sidebar of folders nobody named.
* The Graph container model (exactly one container per message) and the
  ``_patch`` header allowlist that keeps a caller from overriding Authorization.
"""

from __future__ import annotations

import copy
import pickle  # ruff: ignore[suspicious-pickle-import]

import httpx
import pytest

from _outlook_harness import (
    INBOX_FOLDER_ID,
    MOCK_TRANSPORT_SENTINEL,
    Recorder,
    _message,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook.provider import (
    GRAPH_BASE,
    LEGACY_FOLDER_PREFIX,
    OutlookProvider,
)

# ---------------------------------------------------------------------------
# G12: FiledMessage must survive copy, deepcopy, and pickle
# ---------------------------------------------------------------------------


def test_filed_message_survives_copy_and_deepcopy() -> None:
    """``__new__`` takes a required kw-only argument, so copy needs the hook.

    Without ``__getnewargs_ex__`` the copy protocol calls ``str.__new__``'s
    replacement with positional args only and raises TypeError, which makes the
    receipt unusable to any caller that stores or duplicates it.
    """
    from inbox_zero_connector.providers.outlook.provider import FiledMessage

    original = FiledMessage("Action", message_id="AAMkAGI2TG93AAA=")

    shallow = copy.copy(original)
    assert str(shallow) == "Action"
    assert shallow.message_id == "AAMkAGI2TG93AAA="

    deep = copy.deepcopy(original)
    assert str(deep) == "Action"
    assert deep.message_id == "AAMkAGI2TG93AAA="


def test_filed_message_survives_a_pickle_round_trip() -> None:
    """A receipt crossing a process boundary must keep its post-move id."""
    from inbox_zero_connector.providers.outlook.provider import FiledMessage

    original = FiledMessage("Action", message_id="moved")
    # Not untrusted input: the object being round-tripped is one this test
    # just constructed, so flake8-bandit's deserialization warning does not
    # describe a risk that exists here.
    restored = pickle.loads(pickle.dumps(original))  # ruff: ignore[suspicious-pickle-usage]

    assert isinstance(restored, FiledMessage)
    assert str(restored) == "Action"
    assert restored.message_id == "moved"


def test_filed_message_carries_the_destination_container_id_through_a_round_trip() -> None:
    """The destination id is a receipt too, so copy and pickle must keep it.

    ``__getnewargs_ex__`` re-supplies the keyword arguments by name; a value
    left out of that dict is silently dropped rather than raising, so the id
    the watcher keys its replay window on would go ``None`` on any copy.
    """
    from inbox_zero_connector.providers.outlook.provider import FiledMessage

    original = FiledMessage("rTech/Action", message_id="moved", destination_id="F_DEST")

    assert copy.deepcopy(original).destination_id == "F_DEST"
    # Same non-risk as the round-trip above: this test constructed the object.
    assert pickle.loads(pickle.dumps(original)).destination_id == "F_DEST"  # ruff: ignore[suspicious-pickle-usage]


def test_filing_reports_the_container_id_it_moved_into_under_a_folder_prefix(
    recorder: Recorder,
) -> None:
    """#173 round 3: the receipt names the CONTAINER, not just the folder name.

    Under a non-empty prefix the applied name is ``prefix + folder`` while the
    caller's taxonomy folder is the raw name, so a watcher resolving the
    destination by display name resolves nothing and marks the moved id in the
    wrong stream's replay window. The id is unambiguous, and the adapter
    already POSTed it as ``destinationId``.
    """
    recorder.folders = [{"id": "F_PREFIXED", "displayName": "rTech/Reference"}]
    provider = _prefixed_provider(recorder, "rTech/")

    filed = provider.file_message("m1", "Reference")

    assert str(filed) == "rTech/Reference"
    assert filed.destination_id == "F_PREFIXED"


# ---------------------------------------------------------------------------
# Round 6 sweep: the same shape on the taxonomy folder path
# ---------------------------------------------------------------------------


def test_a_folder_with_no_id_refuses_instead_of_filing_nowhere(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """SWEEP FINDING: ``_folder_id`` carried the identical defect.

    A listed folder whose ``id`` was empty was cached as a resolution and
    POSTed as ``{"destinationId": ""}``, and ``file_message`` returned
    the destination folder name -- a receipt for a move Graph had no
    destination for.
    Same root as W1: an unresolvable id treated as a resolved one.
    """
    recorder.folders = [{"id": "", "displayName": "Action"}]
    with pytest.raises(ProviderError, match="did not return a usable"):
        provider.file_message("m1", "Action")

    moves = [r for r in recorder.requests if r.url.path.endswith("/move")]
    assert moves == [], "a move was issued with an unresolved destination"


def test_a_created_folder_with_no_id_refuses(provider: OutlookProvider, recorder: Recorder) -> None:
    """The create arm of the same function, which is a separate code path."""
    recorder.create_folder_payload = {"displayName": "Receipts"}
    with pytest.raises(ProviderError, match="did not return a usable"):
        provider.file_message("m1", "Receipts")


def test_an_unresolvable_folder_is_not_cached_as_a_success(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """W1b's sibling: the taxonomy cache must not go sticky either."""
    recorder.folders = [{"id": "", "displayName": "Action"}]
    with pytest.raises(ProviderError):
        provider.file_message("m1", "Action")

    recorder.folders = [{"id": "F_EXIST", "displayName": "Action"}]
    assert provider.file_message("m1", "Action") == "Action"


def test_a_thread_anchor_with_no_id_refuses_as_a_provider_error(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """SWEEP FINDING: ``create_draft`` raised a bare KeyError on a missing id.

    It failed CLOSED -- no draft was created -- but a KeyError is outside this
    adapter's contract, so the tool layer surfaced a crash rather than an
    actionable refusal to the host AI.
    """
    anchor = _message("m1", "First")
    del anchor["id"]
    recorder.thread = [anchor]

    with pytest.raises(ProviderError, match="did not return a usable"):
        provider.create_draft("c_m1", "hello")


# ---------------------------------------------------------------------------
# Folder-name prefix. Verified against a live Exchange Online mailbox on
# 2026-08-18: the adapter hardcoded ``InboxZero-``, so the taxonomy folder
# ``Reference`` arrived as the mailbox folder ``InboxZero-Reference`` and the
# owner got a sidebar of folders nobody had named. The prefix is now a setting
# that defaults to empty.
# ---------------------------------------------------------------------------


def _prefixed_provider(recorder: Recorder, prefix: str | None) -> OutlookProvider:
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(recorder.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL, folder_prefix=prefix)


def test_the_default_prefix_creates_a_folder_named_exactly_the_taxonomy_folder(
    recorder: Recorder,
) -> None:
    """``Reference`` must reach Graph as ``Reference``, not ``InboxZero-Reference``.

    Asserted on the CREATE payload rather than only on the return value,
    because the return value is the receipt and the payload is what the owner
    actually sees in Outlook. A test that checked only the receipt would pass
    against an adapter that named the receipt one thing and the folder another.
    """
    import json as _json

    recorder.folders = []
    provider = _prefixed_provider(recorder, None)
    assert provider.file_message("m1", "Reference") == "Reference"
    create = next(
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    )
    assert _json.loads(create.content)["displayName"] == "Reference"


def test_an_operator_prefix_restores_the_previous_folder_names(recorder: Recorder) -> None:
    """The old behaviour is still reachable, byte-for-byte.

    This is the migration promise: a deployment already carrying
    ``InboxZero-`` folders sets the prefix and keeps filing into the folders it
    already has, instead of growing a second unprefixed set beside them.
    """
    import json as _json

    recorder.folders = []
    provider = _prefixed_provider(recorder, LEGACY_FOLDER_PREFIX)
    assert provider.file_message("m1", "Reference") == "InboxZero-Reference"
    create = next(
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    )
    assert _json.loads(create.content)["displayName"] == "InboxZero-Reference"


def test_the_prefix_matches_an_existing_folder_rather_than_creating_a_second_one(
    recorder: Recorder,
) -> None:
    """A prefixed deployment must RESOLVE its old folder, not duplicate it."""
    recorder.folders = [{"id": "F_LEGACY", "displayName": "InboxZero-Reference"}]
    provider = _prefixed_provider(recorder, LEGACY_FOLDER_PREFIX)
    provider.file_message("m1", "Reference")
    creates = [
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    ]
    assert creates == [], "an existing prefixed folder was resolved, so nothing is created"


def test_the_prefix_comes_from_the_keystone_when_the_adapter_is_given_none(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No constructor override means the SETTING decides, not a hardcoded string.

    ``None`` and ``""`` are different answers -- empty is the default prefix and
    None means "ask config" -- so this proves the sentinel is not collapsing
    them, which a plain falsy check would.
    """
    import json as _json

    from inbox_zero_connector.config import ENV_PREFIX, reset_settings

    monkeypatch.setenv(f"{ENV_PREFIX}FOLDER_PREFIX", "Filed/")
    reset_settings()
    recorder.folders = []
    provider = _prefixed_provider(recorder, None)
    assert provider.file_message("m1", "Reference") == "Filed/Reference"
    create = next(
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    )
    assert _json.loads(create.content)["displayName"] == "Filed/Reference"


# ---------------------------------------------------------------------------
# Paged mailFolders. Graph returns AT MOST ``$top`` folders per response and
# hands the rest back behind ``@odata.nextLink``; a mailbox with more than
# MAX_PAGE_SIZE top-level folders is ordinary, not exotic. ``_folder_id`` GETs
# me/mailFolders exactly once and scans that single page, so everything past
# the first page is invisible to folder resolution.
# ---------------------------------------------------------------------------

PAGE_2_LINK = "https://graph.microsoft.invalid/v1.0/me/mailFolders?$top=100&$skip=100"
"""The absolute continuation url, replayed VERBATIM the way Graph requires.

An absolute link on a host the adapter never configured, on purpose: it proves
the follow-up request came from replaying the server's own link rather than
from the adapter re-deriving a url with a ``$skip`` it guessed at.
"""


def _paged_folders(
    recorder: Recorder, page_1: list[dict[str, str]], page_2: list[dict[str, str]]
) -> list[str]:
    """Serve ``me/mailFolders`` as two pages and return the log of GET urls.

    Wraps ``recorder.handle`` rather than replacing it, so every other route
    still behaves and the POST create-folder path stays observable through
    ``recorder.requests``. Page 1 is the response to the parameterless listing
    the adapter issues today; page 2 answers only the exact continuation link,
    which is what makes "did the adapter follow the link" an observable fact
    instead of an inference.
    """
    inner = recorder.handle
    seen: list[str] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/me/mailFolders"):
            seen.append(str(request.url))
            if "$skip" in request.url.params:
                recorder.requests.append(request)
                return httpx.Response(200, json={"value": page_2})
            recorder.requests.append(request)
            return httpx.Response(200, json={"value": page_1, "@odata.nextLink": PAGE_2_LINK})
        return inner(request)

    recorder.handle = _handle  # type: ignore[method-assign]
    return seen


def _reprovision(recorder: Recorder) -> OutlookProvider:
    """A provider bound to ``recorder`` AFTER its handler has been overridden.

    ``httpx.MockTransport`` captures the handler by value at construction, so
    the ``provider`` fixture already holds the original bound method by the time
    a test overrides it. Rebuilding the client is what makes the override take
    effect.
    """
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(recorder.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


def _creates(recorder: Recorder) -> list[httpx.Request]:
    return [
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    ]


def test_a_folder_on_a_later_page_is_found_not_recreated(recorder: Recorder) -> None:
    """Graph PAGES mailFolders, and a match on page 2 was invisible.

    Filing then minted a duplicate ``Action`` beside the one the mailbox
    already had, and every later move targeted whichever of the twins the next
    single-page scan happened to see -- so the owner's filed mail split across
    two identically named folders with no signal that it had.
    """
    seen = _paged_folders(
        recorder,
        page_1=[{"id": "F_OTHER", "displayName": "Reference"}],
        page_2=[{"id": "F_PAGE2_ACTION", "displayName": "Action"}],
    )
    provider = _reprovision(recorder)

    assert provider._folder_id("Action") == "F_PAGE2_ACTION"
    assert _creates(recorder) == [], "a duplicate folder was created for a name page 2 already had"
    assert any("$skip" in url for url in seen), "the @odata.nextLink was never followed"


def test_an_exhausted_folder_listing_still_creates_once(recorder: Recorder) -> None:
    """Exhausting the pages is what licenses the create, and it must happen ONCE.

    Page 2 carries no ``@odata.nextLink``, so the listing is genuinely over and
    creating is correct. Today the create fires after page 1 and page 2 is
    never requested: the POST count alone cannot tell a licensed create from a
    premature one, so the page-2 GET is asserted as well.
    """
    seen = _paged_folders(
        recorder,
        page_1=[{"id": "F_OTHER", "displayName": "Reference"}],
        page_2=[{"id": "F_ELSEWHERE", "displayName": "Receipts"}],
    )
    provider = _reprovision(recorder)

    assert provider._folder_id("Action") == "F_Action"
    assert any("$skip" in url for url in seen), "the create fired before the listing was exhausted"
    assert len(_creates(recorder)) == 1, "the folder was created more than once"
    # Cached: a second resolution must not re-list or re-create.
    assert provider._folder_id("Action") == "F_Action"
    assert len(_creates(recorder)) == 1


def test_a_graph_message_carries_exactly_one_container(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Graph's answer is exactly one folder, and the projection has to ask for it.

    ``Container.exclusive`` is True for Graph: a message sits in a single
    folder. The id and the ``$select`` are one change, not two -- Graph returns
    only the fields named, so a projection without ``parentFolderId`` would
    report every message as placed nowhere.
    """
    # The inbox route is overridden for this test only, rather than the shared
    # fake being changed: the default parent is SENT_FOLDER_ID, and asserting
    # against that would not distinguish "carried the message's own
    # parentFolderId" from "returned a constant that happens to match".
    inbox = [_message("m1", "First", parent_folder_id=INBOX_FOLDER_ID)]
    recorder._inbox = lambda _: httpx.Response(200, json={"value": inbox})  # type: ignore[method-assign]
    messages = provider.list_inbox(limit=10)
    assert messages[0].containers == (INBOX_FOLDER_ID,)
    assert len(messages[0].containers) == 1
    listing = recorder.requests[0]
    assert "parentFolderId" in listing.url.params["$select"]


# -- _patch caller headers ---------------------------------------------------
#
# The parameter exists so a caller can send If-Match, which is Graph's
# concurrency control on a message PATCH (verified live 2026-08-18: a stale
# etag answers 412 and the resource is provably untouched). These tests cover
# the three things that make it safe to have: the header REACHES the request,
# omitting it changes nothing about what was sent before the parameter
# existed, and the client's own Authorization cannot be displaced by a caller.


def test_patch_sends_a_caller_supplied_allowlisted_header(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """An allowed header reaches the wire with the value the caller gave."""
    provider._patch(
        "me", "messages", "m1", payload={"importance": "high"}, headers={"If-Match": 'W/"etag-1"'}
    )
    patch = next(r for r in recorder.requests if r.method == "PATCH")
    assert patch.headers["If-Match"] == 'W/"etag-1"'


def test_patch_without_headers_is_identical_to_before_the_parameter(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The no-headers path sends no If-Match and still carries the client's auth.

    This is the regression guard for the existing caller: ``set_important``
    passes no headers, so its request must be byte-identical to what it was
    when ``_patch`` had no ``headers`` parameter at all.
    """
    provider.set_important("m1", True)
    patch = next(r for r in recorder.requests if r.method == "PATCH")
    assert "If-Match" not in patch.headers
    assert patch.headers["Authorization"] == f"Bearer {MOCK_TRANSPORT_SENTINEL}"
    assert patch.headers["Content-Type"] == "application/json"


def test_patch_refuses_a_caller_attempt_to_override_authorization(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Authorization is not on the allowlist, so the call dies before any request.

    Asserting the request was never MADE, not merely that the header survived:
    a merge-order implementation would also keep the right value here, and the
    point of the allowlist is that the attempt itself is refused.
    """
    with pytest.raises(ProviderError, match="Authorization"):
        provider._patch(
            "me",
            "messages",
            "m1",
            payload={"importance": "high"},
            headers={"Authorization": "Bearer stolen"},
        )
    assert not [r for r in recorder.requests if r.method == "PATCH"]
