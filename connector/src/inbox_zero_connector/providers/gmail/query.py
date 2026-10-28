"""Gmail search-query compilation: the ONE place a selection becomes a query.

Split out of ``gmail.py`` because that module sat at the 500-code-line ceiling.
The split is by MOVING whole functions, never by compressing them: the previous
attempt to make room by rewriting working code shipped a real address-parsing
regression, so the budget now comes from the file boundary instead.

The boundary this module owns is worth stating on its own. ``read_sent_sample``
promises sent mail only, and that promise is carried by an unconditional
``in:sent`` in a query assembled by INTERPOLATION. A dynamic value that
terminates its own term turns everything after it into syntax, which is how
``OR in:anywhere`` gets appended to a query that was supposed to be
unconditional. What keeps the promise true is that ``_sent_query`` interpolates
nothing itself -- every dynamic value goes through ``_clause``, which looks its
validator up in ``_CLAUSE_KINDS`` by the same key that names its template. A
clause kind without a validator cannot be emitted, because there is no other
path to a rendered template.

Each guard here REFUSES rather than escaping, and each refuses on ABSENT
evidence rather than assuming: a label that cannot be proven free of query
syntax is declined, and a date bound that does not parse as a date is declined.
Neither has a fall-through that admits an unexamined value.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from inbox_zero_connector.providers.base import ProviderError, SentSelection
from inbox_zero_connector.providers.dates import parse_iso_date

_LABEL_FORBIDDEN_CHARS = '"'
"""The one character a label NAME may not contain.

Label terms are built as ``label:"<name>"``, and Gmail's search grammar
publishes no escape for a quote INSIDE a quoted term. A name carrying one
closes the term early, and everything after it is parsed as query syntax --
which is how ``OR in:anywhere`` gets appended to a query whose ``in:sent`` was
supposed to be unconditional.

Only the quote is listed, deliberately. Parentheses, braces, ampersands, and
dashes are grouping and operator characters OUTSIDE a quoted term and inert
inside one, and owners' real labels use them: "Client - Acme (2026)", "R&D".
Refusing them would reject legitimate mail to no security benefit, since
without a quote the term cannot be escaped in the first place.
"""

_LABEL_FORBIDDEN_WORDS = ("or", "and")
"""Bare boolean operators. Gmail reads an unquoted uppercase OR/AND between
terms as a boolean, and a label whose name is literally these words has no
legitimate use here."""

_LABEL_FORBIDDEN_PREFIXES = ("in:", "label:", "is:", "has:", "from:", "to:")
"""Search-operator prefixes. A label NAME never legitimately starts a word with
one; a value that does is trying to be a query."""


def _validate_label(label: str, field: str) -> str:
    """Return the trimmed label, or raise if it carries query syntax.

    REFUSAL, not escaping, and the distinction is the whole fix. ``in:sent`` is
    only unconditional if nothing downstream can introduce an ``OR``; a label
    that closes its own quoted term can append ``OR in:anywhere`` and reach
    mail outside the sent folder, which also neutralises the owner's
    exclusions. Gmail offers no escape sequence to defuse that, so the only
    sound answer is to decline the input.

    These are label NAMES, not queries. An owner's real labels look like
    "Client - Acme (2026)" -- ordinary punctuation stays legal; only the
    characters and words that Gmail's grammar reserves are refused.
    """
    cleaned = label.strip()
    # Grouping punctuation is stripped BEFORE the operator test so "{in:anywhere}"
    # is judged on the operator it carries, not excused by the brace in front.
    words = [word.strip("(){}[]") for word in cleaned.lower().split()]
    if any(char in _LABEL_FORBIDDEN_CHARS for char in cleaned) or any(
        word in _LABEL_FORBIDDEN_WORDS or word.lstrip("-").startswith(_LABEL_FORBIDDEN_PREFIXES)
        for word in words
    ):
        raise ProviderError(
            f"Gmail {field} {label!r} cannot contain a quote, a bare boolean, or a "
            "search operator -- a label is a NAME, and one carrying query syntax "
            "would widen the search outside the sent folder. ACTION REQUIRED: pass "
            "the label's actual name."
        )
    return cleaned


def _validate_date(value: str, field: str) -> str:
    """Return an ISO date re-emitted in Gmail's ``YYYY/MM/DD`` form, or raise.

    PARSE, do not pattern-match. ``date.fromisoformat`` accepts a date and
    nothing else, and the string handed back to the query is BUILT FROM THE
    PARSED OBJECT -- so whatever arrived is discarded, not cleaned. There is no
    residue of the input to escape, which is a stronger property than any
    filter: ``'2026-01-01 OR in:anywhere'`` does not parse, and a value that
    does parse cannot contain a space, a quote, or an operator by construction.

    The predecessor was ``value.replace('-', '/')``, which is a substitution on
    an unexamined string; it passed the label arms' hostile payloads straight
    into a query whose ``in:sent`` was supposed to be unconditional.
    """
    # isoformat() on the PARSED object, never the input string: the result
    # is generated from three integers, so nothing of what arrived survives.
    return (
        parse_iso_date(
            value,
            field=f"Gmail {field}",
            hint=(
                "pass the date as YYYY-MM-DD -- one carrying anything else would "
                "widen the search outside the sent folder."
            ),
        )
        .isoformat()
        .replace("-", "/")
    )


_CLAUSE_KINDS: dict[str, tuple[str, Any]] = {
    "label": ('label:"{}"', _validate_label),
    "exclude label": ('-label:"{}"', _validate_label),
    "after": ("after:{}", _validate_date),
    "before": ("before:{}", _validate_date),
}
"""Every clause kind ``_sent_query`` can emit: its template and its validator.

THE BOUNDARY. Round 1 put a guard on the two label call sites and left the two
date ones interpolating raw strings, which is the shape of defect this table
exists to make impossible: a clause kind that is not in here cannot be emitted
at all, and one that is in here cannot be emitted without its validator, because
``_clause`` is the only thing that formats a template and it looks the validator
up by the same key. Adding a clause means adding a row, and a row without a
validator is a KeyError the first test to compile a query raises, rather than a
widened query in production. The key doubles as the field name in the refusal
message, so a row cannot name itself one thing and report another.
"""


def _clause(kind: str, value: str) -> str:
    """Validate one dynamic value and render its clause. The ONLY interpolation.

    Raises:
        ProviderError: The value carried query syntax or was not the shape its
            clause requires. Raised BEFORE any request goes out.
    """
    template, validate = _CLAUSE_KINDS[kind]
    cleaned: str = validate(value, kind)
    return template.format(cleaned) if cleaned else ""


def _sent_query(selection: SentSelection) -> str:
    """Compile a selection into a Gmail search query. Narrowing only.

    Every clause here makes the result set SMALLER. There is no branch that
    can widen it, and ``in:sent`` is unconditional, so no combination of
    inputs produces a query that reaches outside the sent folder.

    That claim depends on every dynamic value being inert, which is not free:
    a clause is assembled by interpolation, so a value that terminates its own
    term turns everything after it into syntax. What keeps the claim true is
    that this function interpolates NOTHING itself -- every dynamic value is
    handed to ``_clause``, which validates by clause kind before rendering. A
    clause added here without going through ``_clause`` has no template to
    render with, so the guard cannot be forgotten one call site at a time,
    which is exactly how the date arms escaped it before.

    Gmail's ``before:`` is exclusive of the named day and ``after:`` is
    inclusive, which matches SentSelection's documented bounds.
    """
    # A blank date bound is ABSENCE, not a value, so it is dropped before the
    # table is consulted rather than validated into an empty clause.
    dates = [("after", selection.after), ("before", selection.before)]
    values = [
        *(("label", label) for label in selection.labels),
        *(("exclude label", label) for label in selection.exclude_labels),
        *((kind, value) for kind, value in dates if value),
    ]
    return " ".join(["in:sent", *filter(None, (_clause(*item) for item in values))])


def log_sent_query(compiled: str) -> None:
    """DEBUG-log a compiled sent query, from OUTSIDE the assembly function.

    Lives here rather than inside ``_sent_query`` because that function's
    structural test refuses ANY f-string in its body -- the same rule that
    keeps unvalidated interpolation out of the query keeps the log line out
    too. The compiled query is owner-supplied selection metadata (labels,
    date bounds), never mailbox content, so DEBUG may show it whole; it is
    how an auditor confirms the narrowing-only claim against a live run.
    """
    logger.debug(f"GMAIL: sent query compiled query={compiled}")


SENT_LABEL = "SENT"
"""Gmail's own label for sent mail. The evidence ``_require_sent`` checks.

Restated here rather than imported from ``gmail`` because that module imports
THIS one; taking the name back the other way would be a circular import. It is
the same literal ``gmail.SENT_LABEL`` holds, and
``test_the_query_module_and_the_adapter_agree_on_the_sent_label`` asserts the
two agree so the duplication cannot drift.
"""

_NOT_SENT_REFUSAL = (
    "is not sent mail, and read_sent_sample returns sent mail only. A message id from "
    "the inbox reaches correspondence the owner RECEIVED, which is outside what this "
    "tool samples. ACTION REQUIRED: pass ids of messages from the sent folder, or "
    "select by label or date range and let the search find them."
)
"""The refusal prose, hoisted so the guard below stays one readable line."""


def _excluded(labels: set[str], exclude: tuple[str, ...]) -> bool:
    """Whether this message carries any label the owner excluded.

    Case-insensitive, and blank entries are ignored so a trailing comma in an
    operator's config does not exclude everything. Exclusions win over
    inclusions -- see ``SentSelection.exclude_labels``.
    """
    lowered = {label.lower() for label in labels}
    return any(item.strip().lower() in lowered for item in exclude if item.strip())


def _require_sent(message_id: str, labels: set[str]) -> None:
    """Raise unless the fetched message carries Gmail's SENT label.

    REFUSAL, not a filter, and the difference is the point. An exclusion is the
    owner narrowing a set they are entitled to read, so dropping those messages
    is doing what they asked. An id outside the sent folder is a caller reaching
    for mail this tool promises never to return -- ``read_sent_sample`` exists
    to sample what the owner WROTE, and the voice profile built from it is only
    meaningful if that holds. Returning fewer messages would honour the contract
    while hiding that the request violated it, so the request is declined and
    named instead.

    The evidence is free: ``labelIds`` arrives on the ``format="full"`` fetch
    that was already happening, so proving a message is sent costs no request
    that was not already in flight.

    Args:
        message_id: The offending id, named in the refusal so the caller can
            find it -- a refusal that does not say WHICH id is unactionable
            when a selection carries several.
        labels: Gmail label ids from the fetched message.

    Raises:
        ProviderError: ``labels`` does not contain SENT.
    """
    if SENT_LABEL not in labels:
        raise ProviderError(f"Gmail message {message_id!r} {_NOT_SENT_REFUSAL}")
