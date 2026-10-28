"""``list_containers`` survives the SAME malformed listing ``find_label_id`` does (#186).

One ``users.labels.list`` payload feeds TWO readers in
``providers/containers.py``, and the first passes of #186 hardened only one of
them. ``find_label_id`` skips a row that is not a mapping; ``gmail_labels_to_
containers`` -> ``gmail_label_to_container`` still called ``raw.get("id", "")``
on whatever the listing held, so a bare string among the rows raised
``AttributeError`` -- the issue's own defect class ("adapter error-contract
leak") on the sibling reader of the identical payload.

THE ASYMMETRY IS THE PROOF IT WAS ONE UNFINISHED FIX. On the very same listing
and the very same provider object, ``file_message`` resolved ``Label_9`` and
filed cleanly while ``list_containers`` died. This file drives BOTH through the
public adapter surface so the pair can never drift apart again.

AND IT DRIFTED AGAIN ONE LEVEL IN (#202), which is why the answer is no longer a
guard per reader. The mapping-check above was matched on both sides, but the ID
was not: the lookup refused a blank or non-``str`` id while this reader ran
``str()`` on it and minted a ``Container`` whose id was ``"['Label_9']"``,
``"7"``, ``"None"``, or ``""``. ``parse_gmail_label_row`` is now the ONE thing
that decides whether a row is usable, and both readers ask it -- the last
section of this file pins the collision that made the drift expensive.

WHY IT MATTERS ABOVE THE BOUNDARY. ``list_containers`` runs on every watch
round (``services/watch/round.py`` container index) and on the reset/fixture
path, and only the daemon has a blanket ``except Exception`` -- so the same
malformed row was a hard crash on one path and an unexplained
``error_type=AttributeError`` backoff on the other, instead of the
``ProviderError`` every caller above the adapter is written to catch. A row we
cannot read cannot be a container, so SKIPPING it is the truthful answer and it
leaves the healthy rows beside it still reported.

No network and no credential: the shared ``FakeService`` replays the listing
off the fixture ``store``.
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

MALFORMED_LISTING: list[Any] = [
    "Reference",
    {"id": "Label_9", "name": "rTech/Reference"},
]
"""The residual's exact store: a bare string beside a perfectly healthy row."""


def test_a_non_dict_listing_row_does_not_crash_list_containers(store: dict[str, Any]) -> None:
    """The refutation, through the public adapter method and no private reach-in.

    Pre-fix this raised ``AttributeError: 'str' object has no attribute 'get'``
    out of ``gmail_label_to_container``, straight through the adapter boundary.
    """
    store["labels"] = list(MALFORMED_LISTING)
    provider = GmailProvider(service=FakeService(store))

    containers = provider.list_containers()

    assert [container.id for container in containers] == ["Label_9"]
    assert [container.name for container in containers] == ["rTech/Reference"]


def test_the_healthy_rows_beside_a_malformed_one_are_still_reported(
    store: dict[str, Any],
) -> None:
    """Skipping is per-ROW, not per-listing: one bad row is not a lost mailbox.

    Refusing the whole enumeration over one unreadable row would lose every
    container the watcher needs to route by, which is a strictly worse answer
    than reporting the rows that ARE readable.
    """
    store["labels"] = [
        {"id": "INBOX", "name": "INBOX"},
        None,
        {"id": "Label_1", "name": "rTech/Business-Action"},
        ["Label_2"],
        {"id": "Label_3", "name": "rTech/Reference"},
    ]

    containers = GmailProvider(service=FakeService(store)).list_containers()

    assert [container.id for container in containers] == ["INBOX", "Label_1", "Label_3"]
    assert containers[0].well_known == "inbox"


def test_a_listing_of_only_malformed_rows_reports_no_containers(
    store: dict[str, Any],
) -> None:
    """The empty answer is truthful, and it is not an exception."""
    store["labels"] = ["Reference", 7, None]

    assert GmailProvider(service=FakeService(store)).list_containers() == []


def test_the_two_readers_of_one_listing_agree(store: dict[str, Any]) -> None:
    """The asymmetry that proved the fix unfinished, pinned as a test.

    ``find_label_id`` (via filing) and ``gmail_labels_to_containers`` (via
    ``list_containers``) read the SAME payload. Before this fix one of them
    resolved ``Label_9`` while the other crashed on the row above it.
    """
    store["labels"] = list(MALFORMED_LISTING)
    provider = GmailProvider(service=FakeService(store))

    listed = provider.list_containers()

    assert [container.id for container in listed] == ["Label_9"]


# ---------------------------------------------------------------------------
# The TOP-LEVEL response object, not the rows inside it (#186).
# ---------------------------------------------------------------------------


class _CannedLabels:
    """``users.labels()`` replaying one literal payload, whatever its shape.

    The shared ``FakeService`` store holds the ROWS and the harness wraps them
    in ``{"labels": ...}``, so it cannot express a response that is not an
    object at all. That is the shape under test here, so this fake replaces the
    labels resource only -- everything else about the adapter is the real one.
    """

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def list(self, **kwargs: Any) -> Any:
        return _Canned(self._payload)


class _Canned:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class _CannedUsers:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def labels(self) -> _CannedLabels:
        return _CannedLabels(self._payload)


class _CannedService:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def users(self) -> _CannedUsers:
        return _CannedUsers(self._payload)


NON_OBJECT_RESPONSES: list[Any] = [
    pytest.param(None, id="response-is-json-null"),
    pytest.param("labels", id="response-is-a-json-string"),
    pytest.param(7, id="response-is-a-json-number"),
    pytest.param([{"id": "L1", "name": "Reference"}], id="response-is-a-json-array"),
]
"""Every non-object shape a JSON transport can put where the response goes."""


@pytest.mark.parametrize("payload", NON_OBJECT_RESPONSES)
def test_a_non_object_labels_response_refuses_on_list_containers(payload: Any) -> None:
    """``dict(listing)`` was a coercion, not a guard.

    ``null`` and a number raised ``TypeError: 'NoneType' object is not
    iterable``, and a string raised ``ValueError: dictionary update sequence
    element #0 has length 1; 2 is required``. Neither is ``ProviderError``, so
    both left the adapter as the issue's own "error-contract leak" -- on the
    method the watcher calls every round.
    """
    with pytest.raises(ProviderError):
        GmailProvider(service=_CannedService(payload)).list_containers()


@pytest.mark.parametrize("payload", NON_OBJECT_RESPONSES)
def test_the_same_response_refuses_on_the_filing_path_too(payload: Any) -> None:
    """One call, two readers: ``_labels_listing`` is shared, so the guard is too."""
    with pytest.raises(ProviderError):
        GmailProvider(service=_CannedService(payload)).file_message("m1", "Reference")


def test_a_json_array_of_pairs_is_refused_rather_than_coerced() -> None:
    """The worst shape: it raised NOTHING and produced a table Gmail never sent.

    ``[("labels", [...])]`` is exactly what ``dict()`` accepts, so the adapter
    FABRICATED a container -- a filing destination -- out of a malformed
    response. A wrong answer is worse than a crash here, because
    ``list_containers`` feeds the watcher's routing index.
    """
    payload = [("labels", [{"id": "L1", "name": "Reference"}])]

    with pytest.raises(ProviderError):
        GmailProvider(service=_CannedService(payload)).list_containers()


def test_the_refusal_names_the_operation_and_not_the_response() -> None:
    """The caller is diagnosable; the response body is mailbox content."""
    with pytest.raises(ProviderError) as caught:
        GmailProvider(service=_CannedService("SECRET-LABEL-NAME")).list_containers()

    rendered = str(caught.value)
    assert "list_containers" in rendered
    assert "SECRET-LABEL-NAME" not in rendered


def test_a_healthy_response_object_is_returned_unchanged() -> None:
    """The negative control: the shape check does not touch a real response."""
    payload = {"labels": [{"id": "L1", "name": "rTech/Reference"}]}

    containers = GmailProvider(service=_CannedService(payload)).list_containers()

    assert [container.id for container in containers] == ["L1"]


# ---------------------------------------------------------------------------
# The ``labels`` KEY between the envelope and the rows (#186 residual).
#
# The two guards above hardened the top-level object and each individual row.
# The value of the ``labels`` key itself -- what both readers iterate -- was
# still read raw, so every non-array JSON value it can legally hold was
# unhandled one level in from a guard that exists for exactly this harm.
# ---------------------------------------------------------------------------

SCALAR_LABELS_KEYS: list[Any] = [
    pytest.param(7, id="labels-is-a-json-number"),
    pytest.param(True, id="labels-is-a-json-bool"),
    pytest.param(1.5, id="labels-is-a-json-float"),
]
"""Shapes that LEAKED ``TypeError: 'int' object is not iterable``."""

UNREADABLE_LABELS_KEYS: list[Any] = [
    pytest.param({"Label_9": {"id": "Label_9", "name": "Reference"}}, id="labels-is-a-json-object"),
    pytest.param("Label_9", id="labels-is-a-json-string"),
]
"""Shapes that raised NOTHING and reported an empty table for a full mailbox."""


def _keyed(labels: Any) -> dict[str, Any]:
    """A store whose ``labels`` key holds ``labels`` verbatim, whatever its shape."""
    return {"labels": labels, "messages": {}, "list": {"messages": []}}


@pytest.mark.parametrize("labels", SCALAR_LABELS_KEYS)
def test_a_scalar_labels_key_refuses_on_list_containers(labels: Any) -> None:
    """RED: ``TypeError``, out of the method the watcher calls every round.

    Not ``ProviderError``, so it sailed past every caller above the adapter
    boundary -- the error-contract leak #186 is titled after, one level in from
    the envelope guard that was written to stop it.
    """
    provider = GmailProvider(service=FakeService(_keyed(labels)))

    with pytest.raises(ProviderError):
        provider.list_containers()


@pytest.mark.parametrize("labels", SCALAR_LABELS_KEYS)
def test_a_scalar_labels_key_refuses_on_the_filing_path_too(labels: Any) -> None:
    """One shared ``_labels_listing`` call, so one guard covers both readers."""
    provider = GmailProvider(service=FakeService(_keyed(labels)))

    with pytest.raises(ProviderError):
        provider.file_message("m1", "Reference")


@pytest.mark.parametrize("labels", UNREADABLE_LABELS_KEYS)
def test_an_unreadable_labels_key_is_refused_not_read_as_an_empty_mailbox(
    labels: Any,
) -> None:
    """The worse half: it raised nothing and answered ``[]`` for a full mailbox.

    Gmail HOLDS ``Reference`` in every one of these payloads. A wrong answer is
    worse than a crash here for the reason ``gmail_listing_or_refuse`` already
    states about the envelope: ``list_containers`` feeds the watcher's routing
    index and the reset snapshot path.
    """
    provider = GmailProvider(service=FakeService(_keyed(labels)))

    with pytest.raises(ProviderError):
        provider.list_containers()


@pytest.mark.parametrize("labels", UNREADABLE_LABELS_KEYS)
def test_an_unreadable_labels_key_does_not_drive_a_create_write(labels: Any) -> None:
    """THE WRITE. An empty routing table is not inert -- it mints a label.

    ``_label_id`` reads "no such label" off the empty answer and fires its
    create-once fallback, so the adapter issued ``users.labels.create`` against
    a mailbox that already held that label. Measured pre-fix: resolved
    ``LBL_DUP`` with one create, against a control that resolves ``Label_9``
    with none.
    """
    store = _keyed(labels)
    store["label_create_hook"] = lambda **kw: {"id": "LBL_DUP", "name": kw["body"]["name"]}
    service = FakeService(store)
    provider = GmailProvider(service=service)

    with pytest.raises(ProviderError):
        provider.file_message("m1", "Reference")

    assert [name for name, _ in service.log if name == "labels.create"] == []


def test_an_absent_labels_key_stays_legal() -> None:
    """Gmail OMITS the key on a mailbox with no labels; refusing it is wrong."""
    assert GmailProvider(service=_CannedService({})).list_containers() == []


def test_a_null_labels_key_stays_legal() -> None:
    """``None`` is the absent key spelled out, and the readers' ``or []`` owns it."""
    assert GmailProvider(service=_CannedService({"labels": None})).list_containers() == []


def test_a_real_labels_array_is_untouched_by_the_key_check() -> None:
    """The negative control: the guard does not narrow a healthy response."""
    payload = {"labels": [{"id": "L1", "name": "rTech/Reference"}]}

    containers = GmailProvider(service=_CannedService(payload)).list_containers()

    assert [container.id for container in containers] == ["L1"]


def test_the_key_refusal_names_the_operation_and_not_the_payload() -> None:
    """The caller is diagnosable; the labels payload is mailbox content."""
    payload = {"labels": {"SECRET-LABEL-NAME": {"id": "L1"}}}

    with pytest.raises(ProviderError) as caught:
        GmailProvider(service=_CannedService(payload)).list_containers()

    rendered = str(caught.value)
    assert "list_containers" in rendered
    assert "SECRET-LABEL-NAME" not in rendered


def test_skipped_rows_are_counted_at_warning_not_only_debug(
    store: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A listing whose rows are ALL unusable was silently empty (#186 residual).

    Per-row DEBUG lines are off at the default level, so the operator saw an
    empty routing table and no reason for it -- and that empty table then drove
    a ``labels.create`` write. The count, the kept/total, and the row TYPES
    ship; the rows never do, because they are mailbox content (#202).
    """
    store["labels"] = ["Label_9", 7, None]
    handler = logger.add(caplog.handler, level="WARNING", format="{message}")
    try:
        with caplog.at_level("WARNING"):
            containers = GmailProvider(service=FakeService(store)).list_containers()
    finally:
        logger.remove(handler)

    assert containers == []
    lines = [
        record.getMessage()
        for record in caplog.records
        if "unusable and skipped" in record.getMessage()
    ]
    assert len(lines) == 1, lines
    assert "skipped=3" in lines[0]
    assert "kept=0" in lines[0]
    assert "row_types=['NoneType', 'int', 'str']" in lines[0]
    assert "Label_9" not in lines[0]


def test_a_healthy_listing_logs_no_skip_warning(
    store: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No false-loud: a clean listing must not teach operators to ignore warnings."""
    store["labels"] = [{"id": "L1", "name": "rTech/Reference"}]
    handler = logger.add(caplog.handler, level="WARNING", format="{message}")
    try:
        with caplog.at_level("WARNING"):
            GmailProvider(service=FakeService(store)).list_containers()
    finally:
        logger.remove(handler)

    assert [
        record.getMessage()
        for record in caplog.records
        if "unusable and skipped" in record.getMessage()
    ] == []


# ---------------------------------------------------------------------------
# BLANK-ID ROWS CANNOT COLLIDE IN THE ROUTING INDEX (#202).
#
# ``str(raw.get("id", ""))`` mapped every blank or non-``str`` id to a string, so
# two unusable rows in one listing produced two containers with the SAME id.
# ``services/watch/round.py:_container_index`` and
# ``services/reset/fixture.py`` both key a dict by ``container.id``, so the
# second row overwrote the first: one real folder disappeared from the index and
# ``_covered`` then refused mail in it as out of scope. This is the harm the
# stringify caused that a per-row skip could not, and it is why the answer is one
# parser rather than another guard.
# ---------------------------------------------------------------------------


def test_blank_id_rows_cannot_collide_in_a_container_index(store: dict[str, Any]) -> None:
    """RED at e406e9c: three rows in, three containers out, two sharing id ``""``."""
    store["labels"] = [
        {"id": "", "name": "rTech/Reference"},
        {"id": "   ", "name": "rTech/Business-Action"},
        {"id": "Label_9", "name": "rTech/Waiting"},
    ]

    containers = GmailProvider(service=FakeService(store)).list_containers()
    index = {container.id: container for container in containers}

    assert [container.id for container in containers] == ["Label_9"]
    assert len(index) == len(containers)


NON_STRING_ID_ROWS: list[Any] = [
    pytest.param(["Label_9"], id="id-is-a-json-array"),
    pytest.param(7, id="id-is-a-json-number"),
    pytest.param(None, id="id-is-json-null"),
    pytest.param(False, id="id-is-json-false"),
    pytest.param("", id="id-is-empty"),
    pytest.param("   ", id="id-is-whitespace"),
]
"""Every id the container reader used to stringify into a filing destination."""


@pytest.mark.parametrize("identifier", NON_STRING_ID_ROWS)
def test_a_row_with_an_unusable_id_is_skipped_by_list_containers(
    store: dict[str, Any], identifier: Any
) -> None:
    """A fabricated container id is a filing destination the mailbox never had."""
    store["labels"] = [
        {"id": identifier, "name": "rTech/Reference"},
        {"id": "Label_9", "name": "rTech/Waiting"},
    ]

    containers = GmailProvider(service=FakeService(store)).list_containers()

    assert [container.id for container in containers] == ["Label_9"]


def test_a_row_whose_name_is_not_a_string_is_skipped_by_list_containers(
    store: dict[str, Any],
) -> None:
    """``str(raw.get("name", ""))`` minted ``"None"``, and scope matches on name."""
    store["labels"] = [
        {"id": "Label_1", "name": None},
        {"id": "Label_9", "name": "rTech/Waiting"},
    ]

    containers = GmailProvider(service=FakeService(store)).list_containers()

    assert [container.id for container in containers] == ["Label_9"]


def test_a_system_label_still_carries_its_well_known_tag(store: dict[str, Any]) -> None:
    """The negative control: the parser does not break the id-keyed tag lookup."""
    store["labels"] = [{"id": "INBOX", "name": "INBOX"}, {"id": "SPAM", "name": "Spam"}]

    containers = GmailProvider(service=FakeService(store)).list_containers()

    assert [container.well_known for container in containers] == ["inbox", "junk"]
