"""``tag_message``: read-union-write on categories, guarded by If-Match.

Split out of ``test_outlook_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns the ``tag_message`` banner and the granted-scope discovery banner.

Graph REPLACES the categories collection on write (live-verified 2026-08-18), so
adding one category is a read, a union, and a full write back under an
``If-Match`` -- these tests are what keep that three-step shape from collapsing
into one clobbering PATCH. The scope-discovery tests are here because they are
the other place the adapter reports what Graph ANSWERED rather than what it
asked for.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from _outlook_harness import (
    MOCK_TRANSPORT_SENTINEL,
    Recorder,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook import reauth as outlook_reauth
from inbox_zero_connector.providers.outlook.provider import (
    OutlookProvider,
)

# ---------------------------------------------------------------------------
# tag_message: read-union-write, guarded by If-Match
#
# The whole reason this path is not one PATCH: Graph REPLACES the categories
# collection on write (live-verified 2026-08-18), so adding one category means
# reading the current set, unioning, and writing the full set back under an
# ``If-Match`` so a concurrent edit cannot be clobbered from a stale read.
# ---------------------------------------------------------------------------


def test_tag_message_preserves_a_category_the_owner_already_set(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """THE data-loss guard. A PATCH replaces, so the union must carry theirs.

    Asserts on the BODY that went to the wire rather than on the return value:
    a implementation that returned the right name while writing only its own
    label would pass a return-value assertion and still destroy ``Client-Acme``.
    """
    recorder.tagged["categories"] = ["Client-Acme"]

    assert provider.tag_message("t1", "Needs-Reply") == "Needs-Reply"

    assert recorder.patch_bodies == [{"categories": ["Client-Acme", "Needs-Reply"]}]
    assert recorder.tagged["categories"] == ["Client-Acme", "Needs-Reply"]


def test_tag_message_issues_no_patch_when_the_label_is_already_present(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A no-op write still costs a request and still bumps everyone's etag."""
    recorder.tagged["categories"] = ["Needs-Reply"]

    assert provider.tag_message("t1", "Needs-Reply") == "Needs-Reply"

    assert recorder.patch_bodies == []
    assert not [r for r in recorder.requests if r.method == "PATCH"]


def test_tag_message_sends_the_etag_from_its_own_read_as_if_match(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The guard is only a guard if the value comes from THIS read.

    Sent verbatim, ``W/`` prefix and quotes included -- the prefix is optional
    to Graph in both directions, so there is no normalising step to get wrong.
    """
    recorder.tagged["@odata.etag"] = 'W/"ETAG-FROM-THE-READ"'

    provider.tag_message("t1", "Needs-Reply")

    assert recorder.if_match_seen == ['W/"ETAG-FROM-THE-READ"']


def test_tag_message_reads_categories_and_nothing_more(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """``$select`` asks for categories only; the etag arrives regardless."""
    provider.tag_message("t1", "Needs-Reply")

    read = next(r for r in recorder.requests if r.method == "GET")
    assert read.url.params["$select"] == "categories"


def test_tag_message_retries_a_412_with_a_freshly_read_etag(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A 412 means our view is stale, so the retry must RE-READ, not replay.

    The recorder mutates the message between the two attempts exactly as a
    concurrent Outlook edit would. A retry that replayed the first body would
    write ``["Needs-Reply"]`` and destroy ``Added-By-Outlook`` -- the clobber
    the whole design exists to prevent -- so the second body is the assertion
    that matters, not the attempt count.
    """
    recorder.patch_status = [412]

    def concurrent_edit(request: httpx.Request) -> None:
        """Somebody else writes AFTER our first read, which is what causes the 412.

        Keyed on the first PATCH having already gone out, so the first attempt
        sees the original state and only the RETRY sees theirs -- fire it any
        earlier and the test proves nothing about re-reading.
        """
        if request.method == "GET" and recorder.patch_bodies:
            recorder.tagged["categories"] = ["Added-By-Outlook"]
            recorder.tagged["@odata.etag"] = 'W/"ETAG-AFTER-THEIR-WRITE"'

    recorder.before = concurrent_edit

    provider.tag_message("t1", "Needs-Reply")

    assert recorder.if_match_seen == ['W/"ETAG-1"', 'W/"ETAG-AFTER-THEIR-WRITE"']
    assert recorder.patch_bodies[-1] == {"categories": ["Added-By-Outlook", "Needs-Reply"]}


def test_tag_message_does_not_retry_a_400(provider: OutlookProvider, recorder: Recorder) -> None:
    """400 is a MALFORMED If-Match -- our own bug. A loop would spin on it.

    Distinct from 412 by STATUS, never by the error-code string: the live run
    returns ``ErrorInvalidChangeKey`` here and ``ErrorIrresolvableConflict``
    there, and a branch keyed on "not 200" would retry our own defect three
    times and then report contention that never happened.
    """
    recorder.patch_status = [400, 400, 400]

    with pytest.raises(ProviderError, match="400"):
        provider.tag_message("t1", "Needs-Reply")

    assert len(recorder.patch_bodies) == 1


def test_tag_message_raises_when_the_attempt_budget_is_spent(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Exhaustion must RAISE. A silent success would make the ledger lie.

    Never a forced unguarded write: dropping ``If-Match`` to push it through
    is exactly the lost update the header was added to prevent.
    """
    from inbox_zero_connector.providers.retry import MAX_ATTEMPTS

    recorder.patch_status = [412] * MAX_ATTEMPTS

    with pytest.raises(ProviderError, match="categories changed in Outlook"):
        provider.tag_message("t1", "Needs-Reply")

    assert len(recorder.patch_bodies) == MAX_ATTEMPTS
    assert all(header is not None for header in recorder.if_match_seen)


def test_tag_message_refuses_a_comma_before_any_request(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Outlook SPLITS a comma-bearing name into two categories, silently.

    Refused before the read, so a name headed for rejection costs no request
    at all -- and the validated, stripped value is what gets applied.
    """
    from inbox_zero_connector.services.triage.taxonomy import LabelNameError

    with pytest.raises(LabelNameError, match="forbidden character"):
        provider.tag_message("t1", "Needs,Reply")

    assert recorder.requests == []


def test_tag_message_applies_the_stripped_name_not_the_argument(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The strip is a CORRECTION. Writing the raw argument would mint a label
    that looks identical in Outlook and compares unequal to everything."""
    assert provider.tag_message("t1", "  Needs-Reply  ") == "Needs-Reply"
    assert recorder.patch_bodies == [{"categories": ["Needs-Reply"]}]


def test_tag_message_never_sends_parent_folder_id(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Graph IGNORES ``parentFolderId`` on PATCH and answers 200 anyway.

    So a stray field would look successful while doing nothing, and the
    no-move guarantee here is code-layer rather than scope-layer. The field
    never appears in a body this method builds.
    """
    recorder.tagged["categories"] = ["Client-Acme"]

    provider.tag_message("t1", "Needs-Reply")

    assert all("parentFolderId" not in body for body in recorder.patch_bodies)
    assert not [r for r in recorder.requests if r.url.path.endswith("/move")]


# ---------------------------------------------------------------------------
# Granted-scope discovery: the ANSWER, never the ask
# ---------------------------------------------------------------------------


class _FakeMsalApp:
    """Stands in for msal.PublicClientApplication with a canned token result."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def get_accounts(self) -> list[dict[str, Any]]:
        return [{"username": "owner@example.com"}]

    def acquire_token_silent(self, scopes: Any, account: Any) -> dict[str, Any]:
        return self._result


def _authenticate_with(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> OutlookProvider:
    """Drive OutlookProvider.authenticate against a canned msal result.

    Patches the msal module object the adapter imports lazily, plus the two
    credential-store calls, so nothing touches a keychain or a network.
    """
    import msal

    monkeypatch.setattr(
        msal, "PublicClientApplication", lambda *a, **k: _FakeMsalApp(result), raising=True
    )
    monkeypatch.setattr(msal, "SerializableTokenCache", _FakeCache, raising=True)
    monkeypatch.setattr(outlook_reauth, "load_token", lambda provider: None)
    monkeypatch.setattr(outlook_reauth, "store_token", lambda provider, payload: None)
    monkeypatch.setattr(
        outlook_reauth, "microsoft_client_config", lambda: ("client-id", "https://login.example")
    )
    provider = OutlookProvider(token=None)
    provider.authenticate(interactive=False)
    return provider


class _FakeCache:
    has_state_changed = False

    def deserialize(self, blob: str) -> None:
        return None

    def serialize(self) -> str:
        return "{}"


def test_outlook_retains_the_scopes_the_msal_result_reports_as_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The granted set comes off the token response, space-delimited."""
    provider = _authenticate_with(
        monkeypatch,
        {
            "access_token": "t",
            "scope": "https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/User.Read",
        },
    )
    assert provider.granted_scopes() == (
        "https://graph.microsoft.com/Mail.ReadWrite",
        "https://graph.microsoft.com/User.Read",
    )


def test_outlook_reports_not_discoverable_when_the_result_has_no_scope_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``scope`` key is UNDETERMINED (None), never an empty grant.

    msal falls back to the requested scopes in this case; this adapter must
    not, because that fallback would report our own ask as the tenant's answer.
    """
    provider = _authenticate_with(monkeypatch, {"access_token": "t"})
    assert provider.granted_scopes() is None


def test_outlook_returns_graph_scopes_raw_without_stripping_the_resource_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalization belongs to grants._normalize, not to the adapter."""
    provider = _authenticate_with(
        monkeypatch, {"access_token": "t", "scope": "https://graph.microsoft.com/Mail.ReadWrite"}
    )
    granted = provider.granted_scopes()
    assert granted is not None
    assert granted[0].startswith("https://graph.microsoft.com/")


def test_outlook_never_reports_the_requested_scope_manifest_as_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrower tenant answer must survive; GRAPH_SCOPES must not leak in."""
    provider = _authenticate_with(
        monkeypatch, {"access_token": "t", "scope": "https://graph.microsoft.com/User.Read"}
    )
    assert provider.granted_scopes() == ("https://graph.microsoft.com/User.Read",)


def test_an_unauthenticated_outlook_adapter_reports_not_discoverable() -> None:
    """An injected-token adapter has seen no token response, so it knows nothing."""
    assert OutlookProvider(token=MOCK_TRANSPORT_SENTINEL).granted_scopes() is None


def test_outlook_satisfies_the_scope_source_protocol_structurally() -> None:
    from inbox_zero_connector.services.auth.grants import ScopeSource

    assert isinstance(OutlookProvider(token=MOCK_TRANSPORT_SENTINEL), ScopeSource)
