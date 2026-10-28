"""``find_label_id`` keeps its errors inside the adapter's error contract (#186).

The function reads a raw ``users.labels.list`` payload, which means it is one of
the places where a MALFORMED provider response reaches this package's code
directly. Every other failure out of the provider layer is a ``ProviderError``,
and callers -- the server's fail-open translation, the adapters' retry
boundaries -- key on exactly that type. A bare ``KeyError`` escaping here is
therefore not merely an uglier message: it is an exception nothing above catches,
so a single label row missing ``id`` turns a recoverable filing failure into an
unhandled crash. Same contract class as #166, which fixed it for the classifier.

The realism is low on purpose -- Gmail is not expected to return a row without
an id -- and that is the argument FOR the guard rather than against it: the case
that never happens in testing is the one whose blast radius nobody has measured.

AN ID THAT IS NOT A STRING IS ALSO MISSING. The blank-id guard reasoned about
``""`` only, so ``id: ["Label_9"]`` passed and was STRINGIFIED to the literal
``"['Label_9']"``, which then travelled to ``users.messages.modify`` as
``addLabelIds`` beside ``removeLabelIds: ['INBOX']`` and was cached -- the exact
"archives the message while adding a label that does not exist, then repeats it
forever" harm the blank-id guard exists to end, reached with a list instead of
an empty string. ``False`` and ``0`` reached it as ``"False"`` and ``"0"``. The
id is a string in the Gmail Label resource or it is not an id.

No network and no credential. These call the pure mapping function on literal
dicts, which is the whole reason it is a module-level function rather than a
method on the adapter.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.containers import (
    find_label_id,
    gmail_label_to_container,
    gmail_labels_to_containers,
    gmail_listing_or_refuse,
    label_id_or_refuse,
    parse_gmail_label_row,
    usable_label_id,
)

NON_STRING_IDS: list[object] = [["Label_9"], {"v": 1}, False, True, 0, 41, 1.5, ("Label_9",)]
"""Every non-``str`` JSON shape the pre-fix guard stringified instead of refusing."""


def test_an_exact_match_missing_its_id_raises_provider_error() -> None:
    """The probe from #186: a name-matching row with no ``id`` key.

    ``ProviderError`` rather than ``KeyError`` is the entire assertion -- the
    former is inside the contract every caller of the provider layer already
    handles, the latter is not.
    """
    with pytest.raises(ProviderError):
        find_label_id({"labels": [{"name": "Reference"}]}, "Reference")


def test_a_case_variant_match_missing_its_id_raises_provider_error() -> None:
    """The case-folded branch reads ``id`` on its own line and needs the guard too.

    A guard on the exact-match branch alone would leave the variant branch --
    which is the branch that actually fires on the mismatched-capitalisation
    mailbox the case-folding exists for -- still raising ``KeyError``.
    """
    with pytest.raises(ProviderError):
        find_label_id({"labels": [{"name": "reference"}]}, "Reference")


def test_the_error_names_the_label_and_never_the_row() -> None:
    """The message carries the label NAME the caller asked for, and nothing else.

    A label row is provider content: its other fields are the mailbox's, not
    diagnostics, and this package's confidentiality rule is that content travels
    as a bound field or not at all. The name is the caller's OWN taxonomy string,
    so it is safe and it is the only thing that makes the error actionable.
    """
    with pytest.raises(ProviderError) as caught:
        find_label_id(
            {"labels": [{"name": "Reference", "messagesTotal": 41, "type": "user"}]},
            "Reference",
        )
    rendered = str(caught.value)
    assert "Reference" in rendered
    assert "messagesTotal" not in rendered
    assert "41" not in rendered


def test_a_row_missing_id_but_also_missing_the_name_is_untouched() -> None:
    """The guard fires on the MATCH, not on every malformed row in the listing.

    A listing may legitimately carry rows this lookup does not care about;
    raising on one of those would convert "no such label, create it once" into a
    hard failure and break the caller's create-once path on a healthy mailbox.
    """
    assert find_label_id({"labels": [{"name": "Archive"}]}, "Reference") is None


@pytest.mark.parametrize("identifier", NON_STRING_IDS)
def test_an_id_that_is_not_a_string_is_refused_not_stringified(identifier: object) -> None:
    """The residual variant: ``str()`` turned malformed ids into plausible labels.

    ``["Label_9"]`` became the literal ``"['Label_9']"``, a label id that cannot
    exist -- and the caller then ARCHIVED the message while adding it, and
    cached it for every later filing of that folder.
    """
    with pytest.raises(ProviderError):
        label_id_or_refuse({"name": "Reference", "id": identifier}, "Reference")


@pytest.mark.parametrize("identifier", NON_STRING_IDS)
def test_a_matched_row_with_a_non_string_id_refuses_through_the_lookup(
    identifier: object,
) -> None:
    """Both branches of ``find_label_id`` inherit the type check from one helper."""
    with pytest.raises(ProviderError):
        find_label_id({"labels": [{"name": "Reference", "id": identifier}]}, "Reference")


def test_the_refusal_for_a_non_string_id_names_the_label_and_not_the_value() -> None:
    """A malformed id is provider content and never ships in the message.

    The caller's own taxonomy string is what makes the error actionable; the
    row's value is the mailbox's, exactly as for the missing-id refusal above.
    """
    with pytest.raises(ProviderError) as caught:
        label_id_or_refuse({"name": "Reference", "id": ["Label_SECRET"]}, "Reference")

    rendered = str(caught.value)
    assert "Reference" in rendered
    assert "Label_SECRET" not in rendered


def test_a_string_id_is_still_returned_unchanged() -> None:
    """The negative control: the type check does not touch a healthy row."""
    assert label_id_or_refuse({"name": "Reference", "id": "Label_9"}, "Reference") == "Label_9"


NON_MAPPING_LABELS: list[object] = [["Label_9"], None, "Label_9", 7, 1.5, True, ("id", "L1")]
"""Every non-mapping shape a JSON transport can put where a Label resource goes."""


@pytest.mark.parametrize("label", NON_MAPPING_LABELS)
def test_a_label_payload_that_is_not_a_mapping_refuses(label: object) -> None:
    """The guard's FIRST line, which is what makes it the one guard it claims to be.

    ``label: dict[str, Any]`` is a type hint on a function reading raw provider
    JSON, and a hint is not enforced at runtime. Without this check the helper's
    behaviour depended on the caller having validated the payload first -- which
    the listing caller did and the create caller did not.
    """
    with pytest.raises(ProviderError):
        label_id_or_refuse(label, "Reference")


def test_the_non_mapping_refusal_names_the_label_and_not_the_payload() -> None:
    """Same rule as the malformed id: the payload is provider content."""
    with pytest.raises(ProviderError) as caught:
        label_id_or_refuse("Label_SECRET", "Reference")

    rendered = str(caught.value)
    assert "Reference" in rendered
    assert "Label_SECRET" not in rendered


NON_MAPPING_LISTINGS: list[object] = [
    None,
    "labels",
    7,
    [{"id": "L1", "name": "Reference"}],
    [("labels", [{"id": "L1", "name": "Reference"}])],
]
"""Non-object responses, including the array-of-pairs ``dict()`` silently ate."""


@pytest.mark.parametrize("listing", NON_MAPPING_LISTINGS)
def test_a_listing_response_that_is_not_an_object_refuses(listing: object) -> None:
    """``dict(listing)`` leaked ``TypeError``/``ValueError`` -- or worse, succeeded.

    The last parameter is the dangerous one: it coerces cleanly into a labels
    table, so the adapter answered with a container the provider never sent.
    """
    with pytest.raises(ProviderError):
        gmail_listing_or_refuse(listing, "list_containers")


def test_the_listing_refusal_names_the_operation_and_not_the_response() -> None:
    """One shared call, caller-specific message -- matching the ``HttpError`` refusal."""
    with pytest.raises(ProviderError) as caught:
        gmail_listing_or_refuse("SECRET-BODY", "label resolution for 'Reference'")

    rendered = str(caught.value)
    assert "label resolution for 'Reference'" in rendered
    assert "SECRET-BODY" not in rendered


def test_a_healthy_listing_is_returned_unchanged() -> None:
    """The negative control: a real response passes through untouched."""
    listing = {"labels": [{"id": "L1", "name": "Reference"}]}

    assert gmail_listing_or_refuse(listing, "list_containers") == listing


# ---------------------------------------------------------------------------
# The ``labels`` KEY, one level in from the envelope (#186 residual).
#
# The envelope guard above proves the RESPONSE is an object. The value under
# ``labels`` -- what ``gmail_labels_to_containers`` and ``find_label_id`` both
# iterate -- was still read raw, so the guard stopped a malformed response at
# the door and let the same two harms in through the window.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels",
    [
        pytest.param(7, id="number"),
        pytest.param(True, id="bool"),
        pytest.param(1.5, id="float"),
        pytest.param("Label_9", id="string"),
        pytest.param({"Label_9": {"id": "Label_9"}}, id="object"),
    ],
)
def test_a_labels_key_that_is_not_an_array_is_refused(labels: object) -> None:
    """Scalars LEAKED ``TypeError``; a string or object answered ``[]`` silently.

    Both are the harms this function's docstring already rules out for the
    envelope, reached one key in: a non-``ProviderError`` past every caller
    above the adapter, or a fabricated-empty routing table that then drives a
    ``users.labels.create`` write.
    """
    with pytest.raises(ProviderError):
        gmail_listing_or_refuse({"labels": labels}, "list_containers")


def test_an_absent_labels_key_is_legal() -> None:
    """Gmail omits ``labels`` on a mailbox with no labels. Refusing it is wrong."""
    assert gmail_listing_or_refuse({}, "list_containers") == {}


def test_a_null_labels_key_is_legal() -> None:
    """``None`` is the absent key spelled out; the readers' ``or []`` owns it."""
    listing = {"labels": None}

    assert gmail_listing_or_refuse(listing, "list_containers") == listing


def test_an_empty_labels_array_is_legal() -> None:
    """An empty array is a real Gmail answer, not a malformed one."""
    listing: dict[str, object] = {"labels": []}

    assert gmail_listing_or_refuse(listing, "list_containers") == listing


def test_the_key_refusal_names_the_operation_and_not_the_payload() -> None:
    """The labels payload is mailbox content and never ships in the message."""
    with pytest.raises(ProviderError) as caught:
        gmail_listing_or_refuse(
            {"labels": "SECRET-LABEL-NAME"}, "label conflict recovery for 'Reference'"
        )

    rendered = str(caught.value)
    assert "label conflict recovery for 'Reference'" in rendered
    assert "SECRET-LABEL-NAME" not in rendered


# ---------------------------------------------------------------------------
# ONE PARSER, BOTH READERS (#202).
#
# The two readers of one listing disagreed about the same row one level in from
# every guard above: ``find_label_id`` refused a blank or non-``str`` id through
# ``label_id_or_refuse``, while ``gmail_label_to_container`` ran
# ``str(raw.get("id", ""))`` on it and minted a ``Container`` with the id
# ``"['Label_9']"``, ``"7"``, ``"None"``, or ``""``. A fabricated container id is
# a filing destination and a routing-index key, and two blank ones COLLIDED in
# ``services/watch/round.py:_container_index`` -- one real folder vanished and
# ``_covered`` then refused mail in it as out of scope.
#
# The redo is one parser, ``parse_gmail_label_row``, sourcing its id test from
# ``usable_label_id``, which is the same call ``label_id_or_refuse`` makes.
# ---------------------------------------------------------------------------

UNUSABLE_IDS: list[object] = [["Label_9"], 7, None, False, True, 0, 1.5, "", "   ", {"v": 1}]
"""Every shape the old ``str()`` turned into a plausible-looking label id."""


@pytest.mark.parametrize("identifier", UNUSABLE_IDS)
def test_an_unusable_id_is_not_a_usable_id(identifier: object) -> None:
    """The one id test. Returns the VALUE, so a caller cannot re-read the field."""
    assert usable_label_id(identifier) is None


def test_a_real_id_comes_back_unchanged() -> None:
    """The negative control, and the reason this returns a value not a bool."""
    assert usable_label_id("Label_9") == "Label_9"


@pytest.mark.parametrize("identifier", UNUSABLE_IDS)
def test_a_row_with_an_unusable_id_does_not_parse(identifier: object) -> None:
    """RED at e406e9c: these became containers with stringified ids."""
    assert parse_gmail_label_row({"id": identifier, "name": "Reference"}) is None


@pytest.mark.parametrize("identifier", UNUSABLE_IDS)
def test_a_row_with_an_unusable_id_is_not_a_container(identifier: object) -> None:
    """``gmail_label_to_container`` has no id logic left of its own to drift."""
    assert gmail_label_to_container({"id": identifier, "name": "Reference"}) is None


@pytest.mark.parametrize("identifier", UNUSABLE_IDS)
def test_both_readers_of_one_listing_agree_about_one_row(identifier: object) -> None:
    """THE HEADLINE. One row, two readers, one answer: this row is not usable.

    The container reader drops it and the lookup refuses it, which is the one
    judgement that legitimately differs -- a refusal here is what stops the
    caller reading "no such label" and issuing a ``labels.create`` WRITE against
    a mailbox that holds the label.
    """
    listing = {"labels": [{"id": identifier, "name": "Reference"}]}

    assert gmail_labels_to_containers(listing) == []
    with pytest.raises(ProviderError):
        find_label_id(listing, "Reference")


NON_MAPPING_ROWS: list[object] = ["Reference", 7, None, ["Label_9"], 1.5, True]
"""Rows that are not Label resources at all."""


@pytest.mark.parametrize("row", NON_MAPPING_ROWS)
def test_a_row_that_is_not_a_mapping_does_not_parse(row: object) -> None:
    """``raw.get`` on a bare string raised ``AttributeError`` past the boundary."""
    assert parse_gmail_label_row(row) is None


@pytest.mark.parametrize("name", [None, 7, ["Reference"], {"v": 1}, False])
def test_a_row_whose_name_is_not_a_string_does_not_parse(name: object) -> None:
    """``str(raw.get("name", ""))`` minted ``"None"`` and ``"['Reference']"``.

    A display name Gmail never sent is not merely cosmetic: ``MailboxScope``
    matches folders by name, so a fabricated one is a scope decision.
    """
    assert parse_gmail_label_row({"id": "Label_9", "name": name}) is None


def test_a_row_with_no_name_key_does_not_parse() -> None:
    """An absent name is a missing name; the old default invented ``""``."""
    assert parse_gmail_label_row({"id": "Label_9"}) is None


def test_a_blank_name_is_legal_where_a_blank_id_is_not() -> None:
    """A name is displayed and matched; an id is WRITTEN to the mailbox."""
    row = parse_gmail_label_row({"id": "Label_9", "name": ""})

    assert row is not None
    assert (row.id, row.name) == ("Label_9", "")


def test_a_healthy_row_parses_to_its_two_fields() -> None:
    """The negative control: the parser does not narrow a real Label resource."""
    row = parse_gmail_label_row({"id": "Label_9", "name": "rTech/Reference", "type": "user"})

    assert row is not None
    assert (row.id, row.name) == ("Label_9", "rTech/Reference")


def test_a_name_that_is_not_a_string_can_never_case_match() -> None:
    """``str(label_name).casefold()`` let ``name: None`` match a folder "none"."""
    assert find_label_id({"labels": [{"id": "L1", "name": None}]}, "None") is None
