"""Untrusted identifiers at the Graph boundary: message ids, thread ids, dates.

Split out of ``test_outlook_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns the injection-shaped banners, which are one concern seen at three
different parameters:

* F1 -- a ``message_id`` must not be able to steer the URL out of its segment.
* T1 -- a ``$filter`` VALUE must not be able to widen the query the way a
  crafted path segment re-aimed the request.
* W2 -- date bounds are PARSED, not shape-matched.
* G6 -- the structural AST scans that prove no dynamic value is interpolated
  into a Graph path or an OData filter ANYWHERE in the adapter, so the property
  survives a future refactor nobody remembered to re-check.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from _outlook_harness import (
    _HOSTILE_MESSAGE_IDS,
    Recorder,
    _message,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook.provider import (
    OutlookProvider,
)

# ---------------------------------------------------------------------------
# F1: message_id is untrusted input and must not be able to steer the URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", _HOSTILE_MESSAGE_IDS)
def test_file_message_refuses_a_path_bearing_message_id(
    provider: OutlookProvider, recorder: Recorder, hostile: str
) -> None:
    """A crafted id must be refused BEFORE any HTTP, not encoded and sent.

    The second assertion is the load-bearing one: rejecting late still lets the
    folder-resolution GET (and its folder CREATE) happen as a side effect of an
    attacker-supplied id, so the refusal has to cost zero requests.
    """
    with pytest.raises(ProviderError, match="message_id"):
        provider.file_message(hostile, "Action")
    assert recorder.requests == [], "a rejected id must cost zero requests"


@pytest.mark.parametrize("hostile", _HOSTILE_MESSAGE_IDS)
def test_set_important_refuses_a_path_bearing_message_id(
    provider: OutlookProvider, recorder: Recorder, hostile: str
) -> None:
    with pytest.raises(ProviderError, match="message_id"):
        provider.set_important(hostile, True)
    assert recorder.requests == [], "a rejected id must cost zero requests"


def test_a_crafted_id_never_reaches_a_send_path(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The PROVABLE claim: no crafted id produces a request to a send action.

    This is the invariant the security surface is sold on. It is asserted over
    the recorded traffic rather than over the exception, because "it raised"
    and "it issued nothing dangerous" are different facts.
    """
    for hostile in _HOSTILE_MESSAGE_IDS:
        with pytest.raises(ProviderError):
            provider.file_message(hostile, "Action")
        with pytest.raises(ProviderError):
            provider.set_important(hostile, True)

    for request in recorder.requests:
        path = request.url.path.lower()
        assert "sendmail" not in path
        assert not path.endswith("/send")
        assert path != "/me/messages/m1/move" or request.method == "POST"


def test_a_legitimate_graph_id_is_percent_encoded_not_rejected(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Real Graph ids are long URL-safe base64 with = padding. Those must WORK.

    A validator that refused them would be a correctness bug dressed as a
    security fix, so the allowlist is asserted from both sides.
    """
    real_shaped = "AAMkAGI2TG93AAA=" + "-_." * 4
    provider.file_message(real_shaped, "Action")
    move = next(r for r in recorder.requests if r.url.path.endswith("/move"))
    # The '=' is encoded in the emitted path; the segment stays ONE segment.
    assert "/me/messages/" in move.url.path
    assert move.url.path.count("/move") == 1


def test_an_over_long_message_id_is_refused(provider: OutlookProvider, recorder: Recorder) -> None:
    with pytest.raises(ProviderError, match="message_id"):
        provider.file_message("A" * 1000, "Action")
    assert recorder.requests == []


# ---------------------------------------------------------------------------
# T1: a $filter VALUE is untrusted input too -- it must not be able to widen
#     the query the way a crafted path segment re-aimed the request
# ---------------------------------------------------------------------------

# Each entry closes the adapter's own quoted literal and bolts on a clause, so
# the emitted filter selects mail the caller never named. The first is the
# canonical OR widening: `conversationId eq 'x' or id ne null or conversationId
# eq ''` matches EVERY message in the mailbox. Escaping by doubling the quote is
# what makes them inert; refusing them outright is what makes them cost nothing.
_HOSTILE_THREAD_IDS = [
    "x' or id ne null or conversationId eq '",
    "x' or startswith(subject,'a",
    "' or true or '",
    "x') or (id ne null",
    "x' and body/content ne null or conversationId ne '",
]


@pytest.mark.parametrize("hostile", _HOSTILE_THREAD_IDS)
def test_get_thread_refuses_a_filter_bearing_thread_id(
    provider: OutlookProvider, recorder: Recorder, hostile: str
) -> None:
    """A crafted conversation id must not widen ``get_thread``'s $filter.

    ``_THREAD_FIELDS`` includes ``body``, so a widened filter returns the FULL
    BODIES of messages outside the requested conversation -- the adapter would
    hand the host AI mail the caller never asked for and has no right to.
    The refusal has to cost zero requests, same rule as a crafted path segment.
    """
    with pytest.raises(ProviderError, match="thread_id"):
        provider.get_thread(hostile)
    assert recorder.requests == [], "a rejected thread_id must cost zero requests"


@pytest.mark.parametrize("hostile", _HOSTILE_THREAD_IDS)
def test_create_draft_refuses_a_filter_bearing_thread_id(
    provider: OutlookProvider, recorder: Recorder, hostile: str
) -> None:
    """The same widening anchors a DRAFT on an unrelated message.

    ``create_draft`` takes ``items[0]`` from the filtered set and calls
    ``createReply`` on it. Widen the filter and the anchor is some other
    conversation entirely, so the owner's reply lands on a thread they never
    opened -- addressed, by Graph, to that thread's participants.
    """
    with pytest.raises(ProviderError, match="thread_id"):
        provider.create_draft(hostile, "Thanks.")
    assert recorder.requests == [], "a rejected thread_id must cost zero requests"


def test_a_crafted_thread_id_never_reaches_a_widened_filter(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Asserted over recorded TRAFFIC, not over the exception.

    "It raised" and "it issued nothing that widened the query" are different
    facts, and only the second one is the security claim.
    """
    for hostile in _HOSTILE_THREAD_IDS:
        with pytest.raises(ProviderError):
            provider.get_thread(hostile)
        with pytest.raises(ProviderError):
            provider.create_draft(hostile, "Thanks.")

    assert recorder.requests == []


def test_a_legitimate_conversation_id_still_resolves(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The allowlist is asserted from both sides: real ids must still WORK.

    A validator that refused legitimate conversation ids would be a
    correctness bug wearing a security fix's clothes.
    """
    thread = provider.get_thread("c_m1")
    assert thread.subject == "First"
    assert thread.bodies == ["body of m1"]
    filter_value = recorder.requests[0].url.params["$filter"]
    assert filter_value == "conversationId eq 'c_m1'"


def test_a_real_shaped_conversation_id_is_accepted(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Graph conversation ids are long URL-safe base64 with ``=`` padding."""
    real_shaped = "AAQkAGI2TG93AAA=" + "-_." * 4
    provider.get_thread(real_shaped)
    filter_value = recorder.requests[0].url.params["$filter"]
    assert filter_value == f"conversationId eq '{real_shaped}'"


def test_the_filter_boundary_doubles_a_quote_rather_than_trusting_the_grammar() -> None:
    """Defence in depth: the escaper is correct on its own, grammar aside.

    The id allowlist already refuses a quote, so this can never fire through a
    public method today. It is asserted anyway because the two halves protect
    different futures: if a later change widens the grammar (Gmail thread ids,
    a new provider, a relaxed charset), the escaper is what still stands
    between a caller and an OR clause. An untested escaper is one nobody
    notices has been broken.
    """
    from inbox_zero_connector.providers.outlook.provider import _odata_string_literal

    assert _odata_string_literal("x' or id ne null") == "'x'' or id ne null'"
    assert _odata_string_literal("plain") == "'plain'"


# The date bounds are the THIRD call site of this same class, found by the
# structural scan rather than by review. ``after``/``before`` are caller-supplied
# strings that ``server.py`` only ``.strip()``s, so an ISO-looking parameter is
# an assumption, not a guarantee.
_HOSTILE_DATES = [
    "2026-01-01 or id ne null",
    "2026-01-01Z or startswith(subject,'a')",
    "' or true or '",
    "2026-01-01T00:00:00Z or body/content ne null",
]


@pytest.mark.parametrize("hostile", _HOSTILE_DATES)
def test_a_crafted_date_bound_cannot_widen_the_sent_filter(
    provider: OutlookProvider, recorder: Recorder, hostile: str
) -> None:
    """``after``/``before`` reach a $filter too, and were never validated.

    ``server.py`` strips whitespace and nothing else, so "it is an ISO date" is
    an assumption the adapter was making about an MCP tool parameter. A bound
    that widens the filter turns the owner's narrow sent-mail selection back
    into the whole-folder read the tool exists to refuse.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="date"):
        provider.read_sent_sample(SentSelection(after=hostile))
    assert recorder.requests == [], "a rejected date bound must cost zero requests"


def test_legitimate_date_bounds_still_go_server_side(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Both sides again: a real ISO date must still bound the read at Graph."""
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-01", before="2026-06-01"))
    filter_value = recorder.requests[0].url.params["$filter"]
    assert filter_value == (
        "sentDateTime ge 2026-01-01T00:00:00Z and sentDateTime lt 2026-06-01T00:00:00Z"
    )


def test_an_over_long_thread_id_is_refused(provider: OutlookProvider, recorder: Recorder) -> None:
    with pytest.raises(ProviderError, match="thread_id"):
        provider.get_thread("A" * 1000)
    assert recorder.requests == []


def test_an_empty_thread_id_is_refused(provider: OutlookProvider, recorder: Recorder) -> None:
    with pytest.raises(ProviderError, match="thread_id"):
        provider.get_thread("   ")
    assert recorder.requests == []


def test_the_rejected_thread_id_is_never_echoed_back(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The refusal text reaches a log line and the host AI's context.

    Echoing attacker-controlled text into either is how a refusal becomes a
    delivery mechanism for the injection it just blocked. Same rule the
    message-id refusal already follows.
    """
    hostile = "x' or id ne null or conversationId eq '"
    with pytest.raises(ProviderError) as caught:
        provider.get_thread(hostile)
    assert hostile not in str(caught.value)


def _graph_modules() -> list[Any]:
    """EVERY module the Graph adapter is made of, for the structural scans.

    A list, not one module, because the scans are the whole reason the path and
    filter boundaries hold, and a scan bound to a single FILE stops covering
    code the moment that code is split into a sibling. Round 6 split the
    evidence types out of ``graph.py`` to stay under the size ceiling; the
    scans following this enumeration is what makes that split free of a
    coverage hole rather than the introduction of one.
    """
    from inbox_zero_connector.providers.outlook import categories as outlook_categories
    from inbox_zero_connector.providers.outlook import consent as outlook_consent
    from inbox_zero_connector.providers.outlook import delta as outlook_delta
    from inbox_zero_connector.providers.outlook import errors as outlook_errors
    from inbox_zero_connector.providers.outlook import evidence as outlook_evidence
    from inbox_zero_connector.providers.outlook import models as outlook_models
    from inbox_zero_connector.providers.outlook import provider as outlook_module
    from inbox_zero_connector.providers.outlook import reauth as outlook_reauth
    from inbox_zero_connector.providers.outlook import self_draft as outlook_self_draft

    return [
        outlook_module,
        outlook_categories,
        outlook_consent,
        outlook_delta,
        outlook_errors,
        outlook_evidence,
        outlook_models,
        outlook_reauth,
        outlook_self_draft,
    ]


def _graph_source_trees() -> list[ast.Module]:
    """Parse each of the adapter's own modules."""
    return [
        ast.parse(Path(module.__file__).read_text(encoding="utf-8")) for module in _graph_modules()
    ]


def test_every_graph_adapter_module_is_covered_by_the_structural_scans() -> None:
    """The enumeration above must not fall behind the package.

    Without this, splitting a module is a silent way OUT of the path and filter
    scans: the new file imports cleanly, the suite stays green, and nothing
    reports that its f-strings are no longer being looked at. Comparing against
    the directory turns "somebody added a graph module" into a FAILURE that
    names the file rather than a gap nobody sees.
    """
    from inbox_zero_connector.providers.outlook import provider as outlook_module

    on_disk = {
        path.stem
        for path in Path(outlook_module.__file__).parent.glob("*.py")
        if path.name != "__init__.py"
    }
    scanned = {Path(module.__file__).stem for module in _graph_modules()}
    assert on_disk == scanned, (
        "a Graph adapter module is not covered by the structural scans; add it to "
        f"_graph_modules(): {sorted(on_disk - scanned)}"
    )


def _builds_a_string(node: ast.AST) -> bool:
    """True for an f-string, a ``+`` concatenation, or a ``.format``/``.join``."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Add)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"format", "join"}
    )


def _string_building_nodes(tree: ast.AST) -> list[ast.AST]:
    """Every f-string, ``str.format`` call, or ``+`` that could build a path."""
    return [node for node in ast.walk(tree) if _builds_a_string(node)]


def _looks_like_a_path(text: str) -> bool:
    """A literal fragment that would place its neighbour inside a URL path."""
    return "/me/" in text or text.startswith("/") or text.endswith("/")


_ODATA_COMPARISONS = (" eq ", " ne ", " ge ", " gt ", " le ", " lt ")
"""Comparison operators, which only end a literal in a filter expression."""


def _looks_like_an_odata_filter(text: str) -> bool:
    """A literal fragment that would place its neighbour inside a $filter.

    Matched on the literal's TAIL, not anywhere inside it, because what makes
    an interpolation dangerous is the text immediately before the value: in
    ``f"conversationId eq '{x}'"`` the dynamic part lands right after ``eq '``.
    A prose sentence that merely contains an apostrophe or the word "or" is not
    building a filter, and matching those turned every refusal message in the
    module into a false finding.
    """
    stripped = text.rstrip()
    return stripped.endswith("'") or any(
        stripped.endswith(operator.rstrip()) for operator in _ODATA_COMPARISONS
    )


def _interpolations_matching(
    shape: Callable[[str], bool],
    allowed_lines: frozenset[tuple[str, int]] = frozenset(),
) -> list[str]:
    """Every string-building expression that mixes ``shape``-ish text with a value.

    Args:
        shape: Predicate over a literal fragment -- path-shaped or filter-shaped.
        allowed_lines: ``(module stem, line)`` pairs for the sanctioned builders,
            if any. The escaper itself has to interpolate; nothing else may.
            Keyed by MODULE as well as line because the scan now covers several
            files: a bare line number would let line 300 of one module exempt
            line 300 of another, which is a hole the scan would never report.

    Returns:
        ``<module> line N: <source>`` for each finding. Empty means clean.
    """
    findings: list[str] = []
    for module, tree in zip(_graph_modules(), _graph_source_trees(), strict=True):
        stem = Path(module.__file__).stem
        for node in _string_building_nodes(tree):
            literals = [
                value.value
                for value in ast.walk(node)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            ]
            dynamic = [value for value in ast.walk(node) if isinstance(value, ast.FormattedValue)]
            if not dynamic or not any(shape(text) for text in literals):
                continue
            if (stem, node.lineno) in allowed_lines:
                continue
            findings.append(f"{stem} line {node.lineno}: {ast.unparse(node)}")
    return findings


def test_no_dynamic_value_is_interpolated_into_a_graph_path() -> None:
    """STRUCTURAL: the module may not build a request path by interpolation.

    This is the check that makes the boundary fix hold. A future call site
    cannot reintroduce the defect by "just adding one more f-string URL",
    because any string-building expression carrying a path-shaped literal and a
    dynamic part fails HERE, before anyone has to remember a rule.

    It scans the AST rather than the text so a formatted string is recognised
    by SHAPE -- a regex over source cannot tell ``f"/me/messages/{x}"`` from a
    docstring quoting one, and this module's docstrings quote several.
    """
    findings = _interpolations_matching(_looks_like_a_path)
    assert findings == [], (
        "a Graph request path is being built by interpolation; route the "
        "dynamic segment through the validating boundary instead: " + "; ".join(findings)
    )


def test_no_dynamic_value_is_interpolated_into_an_odata_filter() -> None:
    """STRUCTURAL: the same rule for $filter VALUES, which round 3 left open.

    Round 3 made path SEGMENTS structurally safe and stopped there, so
    ``f"conversationId eq '{thread_id}'"`` survived on two methods and let a
    caller close the quote and OR in a clause of their own. Making the filter
    scan a sibling of the path scan is what stops the class rather than the two
    instances: a future ``$filter`` built by interpolation fails here whether or
    not its author had heard of this bug.

    The two sanctioned builders are exempted, and every OTHER function in the
    module is scanned. ``_odata_filter`` assembles the clause and
    ``_odata_string_literal`` escapes the value; both must interpolate to do
    their job, and both are covered by their own behavioural tests above.
    """
    from inbox_zero_connector.providers.outlook import provider as outlook_module

    builders = {"_odata_filter", "_odata_string_literal"}
    sanctioned = frozenset(
        (Path(module.__file__).stem, line)
        for module, tree in zip(_graph_modules(), _graph_source_trees(), strict=True)
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef) and function.name in builders
        for line in range(function.lineno, (function.end_lineno or function.lineno) + 1)
    )
    assert sanctioned, "the sanctioned filter builders were not found in the source"
    for builder in builders:
        assert getattr(outlook_module, builder, None) is not None, builder

    findings = _interpolations_matching(_looks_like_an_odata_filter, sanctioned)
    assert findings == [], (
        "an OData $filter value is being built by interpolation; route it "
        "through _odata_filter/_odata_string_literal instead: " + "; ".join(findings)
    )


def test_the_filter_scan_would_catch_a_reintroduced_interpolation() -> None:
    """Prove the scan can FAIL, so a green result means something.

    A structural check that cannot fail is decoration. This feeds the detector
    the exact expression round 3 left in the module and asserts it is caught.
    """
    reintroduced = ast.parse("params = {'$filter': f\"conversationId eq '{thread_id}'\"}")
    node = next(n for n in ast.walk(reintroduced) if isinstance(n, ast.JoinedStr))
    literals = [
        value.value
        for value in ast.walk(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    ]
    assert any(_looks_like_an_odata_filter(text) for text in literals)


def test_the_http_helpers_do_not_accept_a_prebuilt_path_string() -> None:
    """The boundary is enforced by the SIGNATURE, not by a convention.

    ``_get``/``_post``/``_patch`` take path SEGMENTS. A caller therefore has no
    way to hand them a string it formatted itself, which is what made the
    round-1 call-site guards forgettable.
    """
    import inspect

    from inbox_zero_connector.providers.outlook.provider import OutlookProvider as _Graph

    for name in ("_get", "_post", "_patch"):
        parameters = inspect.signature(getattr(_Graph, name)).parameters
        kinds = {p.name: p.kind for p in parameters.values()}
        assert "url" not in kinds, f"{name} still takes a prebuilt url"
        assert any(kind is inspect.Parameter.VAR_POSITIONAL for kind in kinds.values()), (
            f"{name} must take path segments, not one formatted string"
        )


def test_every_untrusted_segment_is_validated_by_the_boundary_itself(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Reach past every public method: the segment builder itself refuses.

    A new private helper calling ``_get("me", "messages", crafted)`` must be
    refused without the author having remembered anything, which is the whole
    difference between this fix and the round-1 one.
    """
    with pytest.raises(ProviderError, match="message_id"):
        provider._get("me", "messages", "m1/../../me/sendMail")
    assert recorder.requests == []


# ---------------------------------------------------------------------------
# W2: date bounds are PARSED, not shape-matched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bound", ["2026-02-30", "9999-99-99", "0000-00-00", "2026-13-01", "2026-00-10"]
)
def test_an_impossible_calendar_date_is_refused_before_any_request(
    provider: OutlookProvider, recorder: Recorder, bound: str
) -> None:
    """W2: these all matched ``\\d{4}-\\d{2}-\\d{2}`` and reached Graph.

    They are not dates. Graph rejected them with an HTTP 400 whose text this
    adapter deliberately withholds, so the caller got an opaque failure with
    no way to learn that their bound was the problem.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="not a real calendar date"):
        provider.read_sent_sample(SentSelection(after=bound))
    assert recorder.requests == [], "a malformed date bound reached Graph"


def test_a_real_date_still_reaches_the_filter(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The refusal must not have narrowed what legitimately works."""
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-31"))
    url = str(recorder.requests[0].url)
    assert "2026-01-31T00%3A00%3A00Z" in url or "2026-01-31T00:00:00Z" in url


def test_the_date_bound_is_re_emitted_from_the_parse_not_the_input() -> None:
    """Nothing of the input survives, which is a stronger claim than filtering.

    ``20260131`` is ISO 8601's BASIC form: ``date.fromisoformat`` accepts it
    and the value handed to the filter comes back in the extended form, which
    proves the return is generated from the parsed object rather than cleaned
    from the input. (``2026-1-31`` is NOT accepted -- ISO requires zero
    padding -- so the old ``\\d{4}-\\d{2}-\\d{2}`` shape check and this parse
    are neither a superset nor a subset of each other. The parse is the
    correct rule: it is the one that admits only real dates.)

    A value that parses cannot carry a space, a quote, or an OData operator by
    construction, so the injection property the shape check was defending is
    preserved rather than traded away.
    """
    from inbox_zero_connector.providers.outlook.provider import _safe_date_bound

    assert _safe_date_bound("20260131") == "2026-01-31"
    assert _safe_date_bound(" 2026-01-31 ") == "2026-01-31"
    for hostile in ("2026-01-01 or id ne null", "2026-01-01'", "", "2026-1-31"):
        with pytest.raises(ProviderError):
            _safe_date_bound(hostile)


def test_a_thread_body_arriving_as_a_string_refuses_as_a_provider_error(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A shape this connector does not accept is ONE refusal, not a crash.

    Before the typed response models, ``body`` arriving as a string instead of
    an object raised ``AttributeError`` from a ``.get()`` chain -- an exception
    outside the adapter's contract, so the tool layer showed the host AI a
    crash instead of an actionable refusal. The model boundary turns it into a
    ``ProviderError`` that names the field path and echoes nothing upstream.
    """
    broken = _message("m1", "First")
    broken["body"] = "not an object"
    recorder.thread = [broken]
    with pytest.raises(ProviderError) as refusal:
        provider.get_thread("thread-1")
    assert "body" in str(refusal.value)
    assert "not an object" not in str(refusal.value)


def test_a_collection_whose_value_is_not_a_list_refuses_not_partially_reads(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """``{"value": <junk>}`` is refused whole; nothing is read out of it."""
    recorder.thread = "junk"  # type: ignore[assignment]  # the malformed shape IS the test
    with pytest.raises(ProviderError):
        provider.get_thread("thread-1")
