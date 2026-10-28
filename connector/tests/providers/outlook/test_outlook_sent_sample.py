"""``read_sent_sample``: the selection grammar and the sent-only contract.

Split out of ``test_outlook_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns three of that file's banners, all of them about the same boundary -- what
``read_sent_sample`` is allowed to return:

* Stage 2 ``read_sent_sample`` -- selection, date bounds, categories, limits.
* G1 -- id validation belongs to the BOUNDARY, so the sent path refuses the same
  hostile message ids the filing path does.
* T2 -- the tool's contract is SENT MAIL ONLY, and the explicit-id path enforces
  that in code rather than assuming it from the endpoint.
* Round 6 -- an unresolvable sent folder must refuse rather than degrade into
  matching everything.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from _outlook_harness import (
    _HOSTILE_MESSAGE_IDS,
    INBOX_FOLDER_ID,
    Recorder,
    _message,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook.provider import (
    OutlookProvider,
)

# ---------------------------------------------------------------------------
# Stage 2: read_sent_sample
# ---------------------------------------------------------------------------


def test_read_sent_sample_refuses_an_empty_selection(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    from inbox_zero_connector.providers.base import ProviderError, SentSelection

    with pytest.raises(ProviderError, match="explicit selection"):
        provider.read_sent_sample(SentSelection())
    assert recorder.requests == [], "an empty selection must cost zero requests"


def test_read_sent_sample_reads_only_the_sent_folder(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-01"))
    assert any("sentitems" in r.url.path for r in recorder.requests)


def test_date_bounds_go_server_side(provider: OutlookProvider, recorder: Recorder) -> None:
    """The read is bounded before it leaves Microsoft, not after."""
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-01", before="2026-06-01"))
    url = str(recorder.requests[0].url)
    assert "sentDateTime" in url
    assert "ge" in url and "lt" in url


def test_selection_categories_filter_the_result(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    recorder.sent = [
        _message("s1", "Re: A", categories=["Clients"]),
        _message("s2", "Re: B", categories=["Internal"]),
    ]
    messages = provider.read_sent_sample(SentSelection(after="2026-01-01", labels=("Clients",)))
    assert [m.id for m in messages] == ["s1"]


def test_exclusions_win_over_inclusions(provider: OutlookProvider, recorder: Recorder) -> None:
    """A message carrying both an included and an excluded category is DROPPED."""
    from inbox_zero_connector.providers.base import SentSelection

    recorder.sent = [_message("s1", "Re: A", categories=["Clients", "Legal"])]
    messages = provider.read_sent_sample(
        SentSelection(after="2026-01-01", labels=("Clients",), exclude_labels=("Legal",))
    )
    assert messages == []


def test_category_matching_is_case_insensitive(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    recorder.sent = [_message("s1", "Re: A", categories=["clients"])]
    messages = provider.read_sent_sample(SentSelection(after="2026-01-01", labels=("Clients",)))
    assert [m.id for m in messages] == ["s1"]


def test_read_sent_sample_returns_bodies_and_recipients(provider: OutlookProvider) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(SentSelection(after="2026-01-01"))
    assert messages[0].body == "body of s1"
    assert messages[0].recipients == ["owner@example.com", "cc@other.example"]


def test_explicit_message_ids_are_fetched_directly(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(SentSelection(message_ids=("s1",)))
    assert [m.id for m in messages] == ["s1"]
    # The message itself is fetched by id, NOT listed out of the sent folder --
    # that is what "directly" means here. The one sentitems request that does
    # happen is the folder-id lookup the sent-only check needs; asserting no
    # sentitems traffic at all was asserting the absence of that check, which
    # is the T2 defect written down as an expectation.
    assert not any(
        r.url.path.endswith("/mailFolders/sentitems/messages") for r in recorder.requests
    )
    assert any(r.url.path.endswith("/me/messages/s1") for r in recorder.requests)


def test_read_sent_sample_clamps_the_limit(provider: OutlookProvider, recorder: Recorder) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-01", limit=100_000))
    url = str(recorder.requests[0].url)
    assert "%24top=200" in url or "$top=200" in url


# ---------------------------------------------------------------------------
# G1: id validation is a property of the BOUNDARY, not of a call site
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", _HOSTILE_MESSAGE_IDS)
def test_read_sent_sample_refuses_a_path_bearing_message_id(
    provider: OutlookProvider, recorder: Recorder, hostile: str
) -> None:
    """The sent-sample path takes ids from the SAME untrusted MCP parameter.

    ``SentSelection.message_ids`` is caller-supplied and reaches a Graph URL,
    so a crafted id here re-aims the request exactly as it did on the file and
    flag paths. Round 1 guarded those two call sites and left this one open,
    which is the reason the guard now lives at the boundary instead.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="message_id"):
        provider.read_sent_sample(SentSelection(message_ids=(hostile,)))
    assert recorder.requests == [], "a rejected id must cost zero requests"


def test_a_crafted_sent_id_never_reaches_a_send_path(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Asserted over recorded TRAFFIC: no crafted sent id produces a send call."""
    from inbox_zero_connector.providers.base import SentSelection

    for hostile in _HOSTILE_MESSAGE_IDS:
        with pytest.raises(ProviderError):
            provider.read_sent_sample(SentSelection(message_ids=(hostile,)))

    assert recorder.requests == []


def test_a_legitimate_sent_id_still_reads_normally(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The allowlist is asserted from both sides here too, not only on /move."""
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(SentSelection(message_ids=("s1",)))
    assert [m.id for m in messages] == ["s1"]
    assert any(r.url.path.endswith("/me/messages/s1") for r in recorder.requests)


# ---------------------------------------------------------------------------
# T2: read_sent_sample promises SENT MAIL ONLY -- the explicit-id path has to
#     enforce that in code, not assume it from the endpoint
# ---------------------------------------------------------------------------


def test_an_inbox_id_handed_to_read_sent_sample_is_refused(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The tool contract says SENT MAIL ONLY. An inbox id must not satisfy it.

    ``list_inbox`` hands the host AI inbox ids, and ``GET /me/messages/{id}``
    resolves any message in the mailbox, so feeding one straight back into
    ``read_sent_sample`` returned an inbox message's FULL BODY under a tool
    whose docstring promises sent mail. That is the sent-only contract failing
    open on the one path that skips the sentitems collection.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="not in the sent"):
        provider.read_sent_sample(SentSelection(message_ids=("i1",)))


def test_the_refusal_of_an_inbox_id_is_explicit_not_silent(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Skipping quietly would be worse than failing.

    A silently dropped id reads to the host AI as "that message had nothing in
    it", so a voice profile would be built from a sample the owner believes is
    complete. The refusal names the id count and says what to do instead.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError) as caught:
        provider.read_sent_sample(SentSelection(message_ids=("i1",)))
    message = str(caught.value)
    assert "ACTION REQUIRED" in message
    assert "sent" in message.lower()


def test_a_genuine_sent_id_still_reads(provider: OutlookProvider, recorder: Recorder) -> None:
    """Both sides of the contract: sent mail must still come back."""
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(SentSelection(message_ids=("s1",)))
    assert [m.id for m in messages] == ["s1"]
    assert messages[0].body == "body of s1"


def test_a_mixed_selection_refuses_rather_than_returning_the_sent_subset(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """One inbox id poisons the batch. Returning the rest hides the refusal.

    Partial success is the silent-skip failure wearing a different hat: the
    caller gets a short list with no signal that anything was dropped.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="not in the sent"):
        provider.read_sent_sample(SentSelection(message_ids=("s1", "i1")))


def test_no_body_of_a_non_sent_message_is_ever_returned(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The claim the tool is sold on, asserted as a property of the OUTPUT.

    "It raised" is not the same fact as "no inbox body reached the caller".
    """
    from inbox_zero_connector.providers.base import SentSelection

    recorder.inbox_only = [_message("i1", "Confidential", parent_folder_id=INBOX_FOLDER_ID)]
    with pytest.raises(ProviderError):
        provider.read_sent_sample(SentSelection(message_ids=("i1",)))

    for request in recorder.requests:
        assert not request.url.path.endswith("/createReply")


def test_the_sent_folder_id_is_resolved_once_across_ids(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The membership check must not cost one folder lookup per message."""
    from inbox_zero_connector.providers.base import SentSelection

    recorder.sent = [_message("s1", "Re: A"), _message("s2", "Re: B")]
    provider.read_sent_sample(SentSelection(message_ids=("s1", "s2")))
    lookups = [
        r
        for r in recorder.requests
        if r.method == "GET" and r.url.path.endswith("/mailFolders/sentitems")
    ]
    assert len(lookups) == 1


def test_a_message_with_no_parent_folder_is_refused(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Absent evidence is not evidence of membership.

    A response missing ``parentFolderId`` cannot be PROVEN to be sent mail, and
    a contract that fails open on missing data is not a contract. Refusing is
    the only answer consistent with the promise on the tool.
    """
    from inbox_zero_connector.providers.base import SentSelection

    unparented = _message("s1", "Re: A")
    del unparented["parentFolderId"]
    recorder.sent = [unparented]

    with pytest.raises(ProviderError, match="not in the sent"):
        provider.read_sent_sample(SentSelection(message_ids=("s1",)))


def test_the_sent_projection_requests_the_parent_folder(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The check needs the field, so the $select has to ask for it.

    Graph returns only what ``$select`` names. Omitting ``parentFolderId``
    would make every message look unparented and refuse the whole path -- the
    field and the check are one change, not two.
    """
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(message_ids=("s1",)))
    fetch = next(r for r in recorder.requests if r.url.path.endswith("/me/messages/s1"))
    assert "parentFolderId" in fetch.url.params["$select"]


# ---------------------------------------------------------------------------
# Round 6: the sent-only guard must not degrade to equality-on-emptiness
# ---------------------------------------------------------------------------


def _unresolvable_sent_folder(recorder: Recorder) -> None:
    """Make ``GET /me/mailFolders/sentitems`` answer 200 with NO usable id.

    Not a contrived shape. Graph answers a ``$select``-less folder GET with
    whatever projection the service decides to send, and a proxy or a partial
    outage can return a well-formed envelope with the id missing. What matters
    is that this is a SUCCESS response the adapter has to interpret, not an
    error status it already refuses.
    """
    recorder.sent_folder_payload = {"displayName": "Sent Items"}


def test_an_unresolvable_sent_folder_refuses_instead_of_matching_everything(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """W1: absent folder id + absent parentFolderId used to compare EQUAL.

    Both halves degraded to ``""`` under the same condition -- Graph not
    supplying the evidence -- so ``"" != ""`` was False and the message was
    ACCEPTED. ``read_sent_sample`` returned the full body of an inbox message
    from a tool whose contract says SENT MAIL ONLY.

    The fix is at the boundary rather than at the comparison: an unresolvable
    folder is not a resolution, so it refuses before any message is judged.
    """
    from inbox_zero_connector.providers.base import SentSelection

    _unresolvable_sent_folder(recorder)
    ghost = _message("i9", "Confidential inbox mail")
    del ghost["parentFolderId"]
    recorder.inbox_only = [ghost]

    with pytest.raises(ProviderError, match="did not return a usable"):
        provider.read_sent_sample(SentSelection(message_ids=("i9",)))


def test_no_body_survives_an_unresolvable_sent_folder(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The OUTPUT property, not just "it raised".

    A guard that raises after handing the body to a caller has still leaked
    it. Asserting on the return value is what pins the promise.
    """
    from inbox_zero_connector.providers.base import SentSelection

    _unresolvable_sent_folder(recorder)
    ghost = _message("i9", "Confidential inbox mail")
    del ghost["parentFolderId"]
    recorder.inbox_only = [ghost]

    returned: list[Any] = []
    with contextlib.suppress(ProviderError):
        returned = list(provider.read_sent_sample(SentSelection(message_ids=("i9",))))
    assert returned == [], "a non-sent body reached the caller"


def test_an_unresolvable_sent_folder_is_not_cached_as_a_success(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """W1b: one bad resolution used to poison the process for its lifetime.

    The cache WRITE was unconditional while the read guard was "key not in
    cache", so ``""`` was stored as though it were an id and every later sent
    read reused it without re-asking Graph. Recovery required a restart.

    Asserted two ways, because either alone is weak: the second call must
    re-issue the lookup (proving nothing was cached), and once Graph answers
    properly the adapter must work (proving the failure was not sticky).
    """
    from inbox_zero_connector.providers.base import SentSelection

    _unresolvable_sent_folder(recorder)
    with pytest.raises(ProviderError):
        provider.read_sent_sample(SentSelection(message_ids=("s1",)))
    with pytest.raises(ProviderError):
        provider.read_sent_sample(SentSelection(message_ids=("s1",)))

    lookups = [
        r
        for r in recorder.requests
        if r.method == "GET" and r.url.path.endswith("/mailFolders/sentitems")
    ]
    assert len(lookups) == 2, "the failed resolution was cached"

    recorder.sent_folder_payload = None
    assert [m.id for m in provider.read_sent_sample(SentSelection(message_ids=("s1",)))] == ["s1"]


def test_an_empty_sent_folder_id_refuses(provider: OutlookProvider, recorder: Recorder) -> None:
    """An explicit empty-string id is the same non-evidence as a missing key."""
    from inbox_zero_connector.providers.base import SentSelection

    recorder.sent_folder_payload = {"id": "", "displayName": "Sent Items"}
    with pytest.raises(ProviderError, match="did not return a usable"):
        provider.read_sent_sample(SentSelection(message_ids=("s1",)))


def test_a_message_with_an_empty_parent_folder_is_refused(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The other half: empty ``parentFolderId`` against a GOOD folder id.

    This is the arm that would silently start passing again if the resolved id
    were ever allowed to be empty -- pinning it means the two halves cannot
    both degrade without a test noticing.
    """
    from inbox_zero_connector.providers.base import SentSelection

    blank = _message("s1", "Re: A", parent_folder_id="")
    recorder.sent = [blank]
    with pytest.raises(ProviderError, match="not in the sent"):
        provider.read_sent_sample(SentSelection(message_ids=("s1",)))


def test_the_resolved_id_type_cannot_hold_an_empty_value() -> None:
    """The invariant is the TYPE, so assert the type rather than a call site.

    A comparison against a value of this type has no degenerate form, which is
    what makes the guard structural instead of remembered.
    """
    from inbox_zero_connector.providers.outlook.evidence import ResolvedId

    for bad in ("", "   ", None, 0, b"x"):
        with pytest.raises(ProviderError):
            ResolvedId(bad, what="test id")
    assert ResolvedId("F_X", what="test id") == "F_X"


def test_absent_evidence_never_equals_a_resolved_id() -> None:
    """``resolved_or_none`` returns None for absence, and None matches nothing."""
    from inbox_zero_connector.providers.outlook.evidence import ResolvedId, resolved_or_none

    resolved = ResolvedId("F_SENT", what="test id")
    for absent in ("", "   ", None, 123):
        assert resolved_or_none(absent, what="test id") != resolved
    assert resolved_or_none("F_SENT", what="test id") == resolved
