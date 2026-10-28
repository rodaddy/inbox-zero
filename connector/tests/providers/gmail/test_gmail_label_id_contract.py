"""Every ``id`` a Gmail label resolution reads is guarded the SAME way (#186).

The first pass of #186 guarded ONE of the two ``id`` reads on this code path --
the lookup branch in ``find_label_id`` -- and left the CREATE branch, which is
the branch that actually fires on the ordinary case: a taxonomy label that does
not exist yet, on a fresh install or a newly added folder. A confirm pass
reproduced a bare ``KeyError: 'id'`` escaping ``_create_label`` on a create
response with no id, which is the exact "adapter error-contract leak" the issue
is titled after, reached through the MORE common branch.

WHAT THIS FILE PINS, four residuals from that pass:

* The create response goes through the same guard as the listing row, so a
  malformed create raises ``ProviderError`` naming the label rather than
  ``KeyError``. One helper, ``containers.label_id_or_refuse``, is the single
  place either branch reads an id, because two guards drift.
* An EMPTY or whitespace-only id is MISSING, not a value. Pre-fix it passed the
  ``is None`` test and reached ``users.messages.modify`` as
  ``{'addLabelIds': [''], 'removeLabelIds': ['INBOX']}`` -- a request that
  ARCHIVES the message (removing INBOX is how Gmail archives) while adding a
  label that does not exist, and the empty string was then cached so every
  later filing of that folder repeated it. A crash is strictly safer than a
  silent wrong write to the user's mailbox.
* A NON-DICT row in the listing is skipped with one DEBUG line instead of
  raising ``AttributeError`` out of ``label.get("name")``. Same leak class as
  the headline defect, three lines above it.
* An exact match still wins even when a MALFORMED case-variant row precedes it
  in the listing. The first fix resolved the variant's id eagerly, so a broken
  row positioned before a healthy exact match aborted a lookup that was
  resolvable -- contradicting the docstring's own promise that "an EXACT match
  still wins over a case variant".

No network and no credential: the listing tests call the pure mapping function
on literal dicts, and the adapter tests drive the shared ``FakeService`` whose
``label_create_hook`` owns the create response.
"""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from _gmail_harness import (
    FakeService,
    GmailProvider,
)
from _gmail_harness import (
    store as store,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.containers import find_label_id, label_id_or_refuse

# ---------------------------------------------------------------------------
# Residual 1 -- the CREATE branch, through the real adapter.
# ---------------------------------------------------------------------------


def test_a_create_response_without_an_id_raises_provider_error(store: dict[str, Any]) -> None:
    """The confirm probe: ``users.labels.create`` answers a row with no ``id``.

    Pre-fix this was ``return str(created["id"])`` and the ``KeyError`` sailed
    past every caller that keys on ``ProviderError``.
    """
    store["labels"] = []
    store["label_create_hook"] = lambda **kwargs: {"name": kwargs["body"]["name"]}
    provider = GmailProvider(service=FakeService(store))

    with pytest.raises(ProviderError) as caught:
        provider.file_message("m1", "Reference")

    assert "Reference" in str(caught.value)


def test_a_create_response_with_an_empty_id_raises_rather_than_archiving(
    store: dict[str, Any],
) -> None:
    """An empty id must never reach ``modify``: that request ARCHIVES the message.

    ``removeLabelIds: ['INBOX']`` is how Gmail archives, so a filing that adds
    ``''`` and removes INBOX takes the message out of the inbox and files it
    nowhere. The assertion is therefore on the CALL LOG as much as the type.
    """
    store["labels"] = []
    store["label_create_hook"] = lambda **kwargs: {"id": "   ", "name": kwargs["body"]["name"]}
    service = FakeService(store)
    provider = GmailProvider(service=service)

    with pytest.raises(ProviderError):
        provider.file_message("m1", "Reference")

    assert not any(name == "messages.modify" for name, _ in service.log), (
        "an unresolved label id must not reach modify -- that request archives"
    )


def test_an_empty_id_is_never_cached(store: dict[str, Any]) -> None:
    """A refused resolution leaves the cache clean, so a retry can still succeed.

    Caching the bad value is what turned one malformed response into a
    permanent per-folder failure in the confirm pass.
    """
    store["labels"] = []
    store["label_create_hook"] = lambda **kwargs: {"id": "", "name": kwargs["body"]["name"]}
    provider = GmailProvider(service=FakeService(store))

    with pytest.raises(ProviderError):
        provider.file_message("m1", "Reference")

    assert provider._label_cache == {}


# ---------------------------------------------------------------------------
# Residual 2 -- empty/whitespace ids in the LISTING branch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("identifier", ["", "   ", "\t\n"])
def test_a_listing_row_whose_id_is_blank_is_treated_as_missing(identifier: str) -> None:
    """Whitespace is not an id. The guard tested ``is None`` and let these past."""
    with pytest.raises(ProviderError):
        find_label_id({"labels": [{"name": "Reference", "id": identifier}]}, "Reference")


def test_a_blank_id_never_reaches_modify_through_the_adapter(store: dict[str, Any]) -> None:
    """The end-to-end shape of the silent wrong-write the confirm pass measured."""
    store["labels"] = [{"id": "", "name": "Reference"}]
    service = FakeService(store)
    provider = GmailProvider(service=service)

    with pytest.raises(ProviderError):
        provider.file_message("m1", "Reference")

    assert not any(name == "messages.modify" for name, _ in service.log)


# ---------------------------------------------------------------------------
# Residual 3 -- a non-dict row in the listing.
# ---------------------------------------------------------------------------


def test_a_non_dict_listing_row_is_skipped_rather_than_crashing() -> None:
    """``label.get("name")`` on a bare string used to raise ``AttributeError``.

    A row we cannot read is a row that cannot be the match, so skipping it is
    the truthful answer: the healthy row beside it still resolves.
    """
    listing = {"labels": ["Reference", {"name": "Reference", "id": "Label_9"}]}

    assert find_label_id(listing, "Reference") == "Label_9"


def test_a_listing_of_only_unreadable_rows_reads_as_no_such_label() -> None:
    """Nothing matched, so the caller's create-once path is the right answer."""
    assert find_label_id({"labels": [None, 41, ["Reference"]]}, "Reference") is None


def test_a_row_that_cannot_match_is_skipped_in_silence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REWRITTEN for #202: the per-row line is gone, and its absence is the rule.

    The old test pinned one DEBUG line per unreadable row inside
    ``find_label_id``. That line was the visible half of the defect: it fired
    where the SIBLING reader of the same listing fired nothing, which is exactly
    the asymmetry #202 removes. A row that is not the name asked for is not a
    finding here -- a listing is FULL of rows this lookup does not want -- and
    the operator-facing report of unusable rows is the counted WARNING that
    ``gmail_labels_to_containers`` emits once per listing, with types and no
    content.
    """
    handler = logger.add(caplog.handler, level="DEBUG", format="{message}")
    try:
        with caplog.at_level("DEBUG"):
            assert find_label_id({"labels": ["Reference"]}, "Reference") is None
    finally:
        logger.remove(handler)

    assert [
        record.getMessage() for record in caplog.records if "CONTAINERS:" in record.getMessage()
    ] == []


# ---------------------------------------------------------------------------
# Residual 4 -- ordering: an exact match wins over a MALFORMED case variant.
# ---------------------------------------------------------------------------


def test_an_exact_match_wins_over_a_malformed_case_variant_before_it() -> None:
    """The variant's id is resolved LAZILY, on the row finally chosen.

    Pre-fix the variant branch called the guard eagerly, so a case-variant row
    with no id positioned BEFORE a healthy exact match raised ``ProviderError``
    on a listing that was fully resolvable -- breaking the function's own
    stated contract that an exact match wins.
    """
    listing = {"labels": [{"name": "reference"}, {"name": "Reference", "id": "Label_9"}]}

    assert find_label_id(listing, "Reference") == "Label_9"


def test_a_malformed_variant_with_no_exact_match_still_refuses() -> None:
    """Laziness must not become silence: the chosen row is still guarded.

    When the variant IS the answer -- no exact match follows it -- the missing
    id is a real malformed response and raises, exactly as before.
    """
    with pytest.raises(ProviderError):
        find_label_id({"labels": [{"name": "reference"}]}, "Reference")


def test_only_the_first_case_variant_is_remembered() -> None:
    """Two variants cannot make the answer depend on which one was malformed."""
    listing = {"labels": [{"name": "reference", "id": "LBL_A"}, {"name": "REFERENCE"}]}

    assert find_label_id(listing, "Reference") == "LBL_A"


# ---------------------------------------------------------------------------
# The shared helper itself.
# ---------------------------------------------------------------------------


def test_the_shared_helper_returns_a_present_id_unchanged() -> None:
    """The negative control: a healthy row is not touched by the guard."""
    assert label_id_or_refuse({"name": "Reference", "id": "Label_9"}, "Reference") == "Label_9"


def test_the_shared_helper_error_names_the_label_and_never_the_row() -> None:
    """A label row is mailbox content; only the caller's own taxonomy string ships."""
    with pytest.raises(ProviderError) as caught:
        label_id_or_refuse({"name": "Reference", "messagesTotal": 41}, "Reference")

    rendered = str(caught.value)
    assert "Reference" in rendered
    assert "messagesTotal" not in rendered
    assert "41" not in rendered


# ---------------------------------------------------------------------------
# Residual 5 -- the CREATE response that is not a MAPPING at all.
# ---------------------------------------------------------------------------

NON_MAPPING_CREATE_RESPONSES: list[Any] = [
    pytest.param(["Label_9"], id="create-returns-a-list"),
    pytest.param(None, id="create-returns-null"),
    pytest.param("Label_9", id="create-returns-a-bare-string"),
    pytest.param(7, id="create-returns-a-number"),
]
"""Every non-object shape a JSON transport can put where a Label resource goes."""


@pytest.mark.parametrize("created", NON_MAPPING_CREATE_RESPONSES)
def test_a_non_mapping_create_response_raises_provider_error(
    store: dict[str, Any], created: Any
) -> None:
    """The residual: the create branch passed its response through UNVALIDATED.

    ``label: dict[str, Any]`` is a type HINT on a function reading raw provider
    JSON, and nothing enforces it at runtime. The listing caller validated the
    row before calling the guard; the create caller did not, so the guard's
    first line -- ``label.get("id")`` -- raised ``AttributeError``, which is not
    ``ProviderError`` and is caught by nothing above the adapter boundary.
    """
    store["labels"] = []
    store["label_create_hook"] = lambda **kwargs: created
    provider = GmailProvider(service=FakeService(store))

    with pytest.raises(ProviderError) as caught:
        provider.file_message("m1", "Reference")

    assert "Reference" in str(caught.value)


@pytest.mark.parametrize("created", NON_MAPPING_CREATE_RESPONSES)
def test_a_non_mapping_create_response_never_reaches_modify(
    store: dict[str, Any], created: Any
) -> None:
    """An unresolved label id must not reach ``modify``: that request ARCHIVES.

    Same reason as the blank-id case above -- ``removeLabelIds: ['INBOX']`` is
    how Gmail archives, so a filing that proceeds on an unresolvable id takes
    the message out of the inbox and files it nowhere.
    """
    store["labels"] = []
    store["label_create_hook"] = lambda **kwargs: created
    service = FakeService(store)
    provider = GmailProvider(service=service)

    with pytest.raises(ProviderError):
        provider.file_message("m1", "Reference")

    assert not any(name == "messages.modify" for name, _ in service.log)
    assert provider._label_cache == {}


def test_the_two_branches_agree_on_the_shape_one_of_them_died_on(
    store: dict[str, Any],
) -> None:
    """The asymmetry that proved the fix unfinished, pinned as a test.

    The SAME bare string ``"Label_9"``: in a LISTING beside a healthy row it is
    skipped and the filing succeeds, and in a CREATE response it used to raise
    ``AttributeError``. Same value, same helper, opposite outcome. Both are now
    ``ProviderError``-or-resolve, so the pair cannot drift apart again.
    """
    store["labels"] = ["Label_9", {"id": "LBL_OK", "name": "Reference"}]
    service = FakeService(store)
    assert GmailProvider(service=service).file_message("m1", "Reference") == "Reference"
    modified = [kwargs for name, kwargs in service.log if name == "messages.modify"]
    assert modified and modified[0]["body"]["addLabelIds"] == ["LBL_OK"]

    store["labels"] = []
    store["label_create_hook"] = lambda **kwargs: "Label_9"
    with pytest.raises(ProviderError):
        GmailProvider(service=FakeService(store)).file_message("m1", "Reference")
