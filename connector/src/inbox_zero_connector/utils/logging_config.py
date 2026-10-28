"""Logging setup and the five-point observability wrapper.

This module owns every logging sink in the process. Nothing else calls
``logger.add()``, ``logger.remove()``, or configures a handler; call sites only
ever call ``logger.debug/info/warning/error`` on the imported loguru logger.
Configuration happens exactly once, at startup, from ``LoggingSettings`` --
which is a section of the ``config`` keystone, so one setting changes every
sink at once.

=============================================================================
STDOUT IS THE MCP PROTOCOL CHANNEL. NOTHING MAY LOG TO IT. EVER.
=============================================================================

The connector speaks MCP over stdio: JSON-RPC frames on stdout, one per line,
read by the host AI's transport. A single stray byte on stdout -- a ``print``,
a library banner, a log line -- lands in the middle of that stream and
corrupts the frame the host is parsing. The failure is not a cosmetic mess: the
transport desynchronises and the connector goes dead mid-conversation, with a
parse error that names the log line rather than the code that emitted it.

So the console sink is on **stderr**, unconditionally, and there is no setting
that moves it. Its LEVEL is separable -- ``logging.console_level`` raises the
console alone, so a support install can keep a DEBUG trace on disk while the
terminal stays quiet -- but the destination is not.

``ruff``'s ``T20`` is selected package-wide to refuse a stray
``print()`` at lint time, with a narrow per-file exemption for the CLI, whose
product genuinely is its stdout and which never runs in the same process as
the server loop.

=============================================================================
NEVER LOG A MESSAGE BODY, SUBJECT, ADDRESS, OR TOKEN
=============================================================================

This connector reads a business owner's mailbox. A log file is a second copy of
whatever it records, in a place with different permissions, a different
lifetime, and a different backup policy than the mailbox -- so logging one
subject line silently creates an unmanaged copy of confidential material. The
observability standard's "never log secrets or private content" is therefore
not advisory here; it is the same confidentiality promise the kit is sold on.

CONTENT TRAVELS AS A BOUND FIELD, NEVER AS MESSAGE PROSE. That single rule is
what the mechanisms below implement, and the reason for it is that redaction by
field NAME is reliable while sanitising free prose is not: there is no way to
tell a mail body from a diagnostic sentence by inspection, so a pass that tried
would blank real diagnostics AND still miss content phrased as a sentence. So
the module does not try. It keeps content out of the message string instead.

Three mechanisms, because prose is not one:

1. **The API only accepts identifiers.** ``ProviderCall`` and ``ToolCall``
   take ``**fields`` that are formatted as ``key=value`` pairs, and the call
   sites pass ids, counts, and booleans. There is no formatting helper that
   takes a body.
2. **The message string is literal-or-identifiers BY CONSTRUCTION.**
   ``find_content_interpolations`` parses every ``*.py`` in this package and
   reports any logging call that builds a content-named value into its message
   -- as an f-string, ``%``-format, or ``str.format``, since the three render
   identically. This is the mechanism that holds the line, and it is enforced
   over the whole package by AST rather than at known call sites, so a file
   that did not exist when the rule was written is covered by it anyway.
   ``test_the_guard_refuses_content_in_a_message_string`` fails the build on a
   finding.
3. **A redacting patcher runs over every record, as a backstop.** ``_redact``
   sweeps BOTH the bound extras and the message string, replacing the value of
   any field whose NAME looks sensitive (case-insensitive, separators
   normalised) with ``[redacted]``, bounded to the depth/width/length limits
   the standard sets. On the message string it covers exactly one shape --
   ``name=value``, including multi-word, quoted, and comma/semicolon-separated
   values -- and it does NOT walk free prose, which is mechanism 2's job.
   Unattributed prose is bounded by length instead: a message past
   ``MAX_MESSAGE_CHARS`` is truncated to a length summary, so a pasted body is
   bounded even though it is unnamed. Since 2026-08-20 it also strips the
   exception message off any attached traceback, because ``opt(exception=True)``
   now actually attaches one.

``tests/test_logging_redaction.py`` sweeps a mocked mailbox and asserts no log
line contains the mock's body, subject, or address strings, and pins mechanisms
2 and 3 against multi-word content directly.

=============================================================================
THE FIVE POINTS
=============================================================================

The observability standard requires entry (debug), exit/result (info), failure
(error, always ``opt(exception=True)``), fallback/degraded (warning), and guard/limit
(warning) on every meaningful operation, and it says plainly to prefer a
wrapper over asking each call site to remember five calls. That is what
``ProviderCall`` and ``ToolCall`` are: context managers that emit entry, exit
with duration, and failure automatically, and expose ``fallback()`` and
``guard()`` for the two paths a wrapper cannot infer.

Every line carries a ``correlation_id``, bound by ``ToolCall`` for the whole
span, so a tool invocation and the provider calls it made can be stitched back
together out of interleaved work.

Key Components:
    - setup: builds all sinks from LoggingSettings. Idempotent.
    - ProviderCall: five-point wrapper for one outbound provider API call.
    - ToolCall: five-point wrapper for one MCP tool invocation.
    - install_crash_handler: logs an unhandled exception before the process dies.

Example:
    >>> from inbox_zero_connector.utils.logging_config import ProviderCall
    >>> with ProviderCall("google", "file_message", message_id="abc") as call:
    ...     call.guard("limit clamped", requested=500, applied=200)

See Also:
    - inbox_zero_connector.config: the only caller of setup()
    - _DOCS/STANDARDS-observability.md
"""

from __future__ import annotations

import ast
import re
import sys
import time
import types
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, Self

from loguru import logger

from inbox_zero_connector.errors import OperatorRefusalError

if TYPE_CHECKING:
    from loguru import Record

    from inbox_zero_connector.config import LoggingSettings

CONSOLE_FORMAT: Final = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[correlation_id]}</cyan> "
    "<level>{message}</level>"
)

FILE_FORMAT: Final = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{extra[correlation_id]} | {name}:{function}:{line} | {message}"
)

DEFAULT_CORRELATION_ID: Final = "-"

SERVICE_NAME: Final = "inbox-zero-connector"

REDACTED: Final = "[redacted]"

MAX_REDACT_DEPTH: Final = 4
"""Container levels a sanitizer descends before summarising. Standard: 4."""

MAX_REDACT_ITEMS: Final = 50
"""Items per container before summarising. Standard: 50."""

MAX_REDACT_CHARS: Final = 200
"""Characters of a retained string. Standard: 200."""

MAX_MESSAGE_CHARS: Final = 500
"""Characters of a rendered message retained before it is truncated.

Higher than ``MAX_REDACT_CHARS`` because a message legitimately carries an
operation name, several ``key=value`` pairs, and an action sentence, where a
single bound VALUE has no reason to be long. A message past this length is not
a diagnostic sentence -- it is content that got pasted into one.
"""

CREDENTIAL_FIELD_PATTERNS: Final[tuple[str, ...]] = (
    # The standard's minimum set.
    "password",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "credential",
    "privatekey",
)
"""Field-NAME fragments naming a CREDENTIAL. Never loggable, suffix or not.

Separated from the content vocabulary because the two answer the id/count
suffix differently, and collapsing them is what made ``token_id`` loggable.

A credential name is unconditional: an id or a count derived from a secret is
still reachable as the secret. ``token_id`` is the id OF a token, and in every
scheme where such an id is ACCEPTED it is a bearer value -- dereferencing it in
a store returns the credential, so logging it is logging the credential with
one indirection. ``password_len`` is worse than useless: it is a search-space
reduction written into a file with a different retention policy than the vault.

CONTENT_FIELD_PATTERNS below answers the same suffix the opposite way, and the
difference is the whole point of the split.
"""

CONTENT_FIELD_PATTERNS: Final[tuple[str, ...]] = (
    # This connector's own private content. A mailbox connector that redacted
    # only credentials would still write the mail itself into the log.
    "body",
    "subject",
    "snippet",
    "address",
    "recipient",
    "sender",
    "participant",
    "email",
    "mail",
    "content",
    "message",
    "text",
    "profile",
)
"""Field-NAME fragments naming this connector's own private CONTENT.

Matched case-insensitively against the name with separators removed, so
``API_KEY``, ``api-key``, and ``apiKey`` are one rule. Substring matching is
deliberate: ``refresh_token``, ``client_secret``, and ``bodyPreview`` must all
match without being enumerated.

Unlike CREDENTIAL_FIELD_PATTERNS, a content name IS exempted by an id or count
suffix: ``message_id`` is an opaque provider handle and ``body_len`` is a size,
and those are exactly what the standard says to log INSTEAD of the value. The
id of a message is not the message; the id of a token is the token.
"""

SENSITIVE_FIELD_PATTERNS: Final[tuple[str, ...]] = (
    *CREDENTIAL_FIELD_PATTERNS,
    *CONTENT_FIELD_PATTERNS,
)
"""Every field-NAME fragment that is sensitive under some spelling.

The union of the two vocabularies, retained because callers and tests treat it
as the denylist's public name. It deliberately does NOT encode precedence --
``is_sensitive_field`` is the only thing that decides, and reading membership
in this tuple as the verdict is the mistake the suffix bug was made of.
"""

ID_FIELD_SUFFIXES: Final[tuple[str, ...]] = ("id", "ids", "count", "len")
"""Name endings that mark a field as an identifier or a count, not content.

Exempts a CONTENT name only. ``message_id`` contains ``message`` and
``body_len`` contains ``body``, and an id or a count is exactly what the
standard says to log instead of the value.

It does NOT exempt a credential name. It used to: the check ran first and
returned False unconditionally, so appending ``_id`` defeated the denylist on
the standard's own minimum set -- ``token_id``, ``password_id``, ``api_key_id``
all returned False and shipped the value to every sink. The suffix rule was
never wrong to exist, only wrong to outrank everything.
"""

_SEPARATORS: Final = re.compile(r"[^a-z0-9]+")


def _normalize_name(name: str) -> str:
    """Lowercase a field name and drop separators, so one rule covers spellings."""
    return _SEPARATORS.sub("", name.lower())


def is_sensitive_field(name: str) -> bool:
    """True when a field NAME must have its value redacted.

    Precedence, highest first -- the ORDER is the rule, not an implementation
    detail:

    1. A CREDENTIAL name is redacted unconditionally. No suffix exempts it,
       because an id or a length derived from a secret still reaches the
       secret: ``token_id`` dereferences to a token wherever it is accepted,
       and ``password_len`` narrows the search space.
    2. An id or count suffix then exempts what is left, so ``message_id`` and
       ``body_len`` are logged -- the opaque handles and sizes the standard
       asks for INSTEAD of values.
    3. Anything else matching the CONTENT vocabulary is redacted, so ``body``
       and ``subject`` never reach a sink.

    Args:
        name: The field name as the call site spelled it. Normalised for case
            and separators, so ``api_key_id``, ``API-KEY-ID``, and ``apiKeyId``
            are one input. Must be a ``str``; any other type RAISES rather than
            returning a quiet ``False``, because silently mis-answering a
            security question is worse than crashing.

    Returns:
        Whether the value must be replaced with ``[redacted]``.
    """
    normalized = _normalize_name(name)
    if any(pattern in normalized for pattern in CREDENTIAL_FIELD_PATTERNS):
        return True
    if normalized.endswith(ID_FIELD_SUFFIXES):
        return False
    return any(pattern in normalized for pattern in CONTENT_FIELD_PATTERNS)


def _redact_value(value: object, depth: int) -> object:
    """Bound and summarise one value. Never falls back to arbitrary ``repr``.

    Args:
        value: The value bound to a log record's extras.
        depth: How many container levels have already been descended.

    Returns:
        A bounded, allowlisted summary of ``value``.
    """
    if isinstance(value, bool | int | float | None.__class__):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_REDACT_CHARS else f"<str len={len(value)}>"
    if depth >= MAX_REDACT_DEPTH:
        return f"<{type(value).__name__} depth-limit>"
    if isinstance(value, dict):
        return _redact_mapping(value, depth + 1)
    if isinstance(value, list | tuple | set) and len(value) > MAX_REDACT_ITEMS:
        return f"<{type(value).__name__} items={len(value)}>"
    if isinstance(value, list | tuple | set):
        return [_redact_value(item, depth + 1) for item in value]
    # Unknown objects and exceptions get a type summary, never a repr: a repr
    # is where a token or a mail body leaks back in through the side door.
    return f"<{type(value).__name__}>"


def _redact_mapping(mapping: dict[Any, Any], depth: int) -> dict[str, object]:
    """Redact a mapping by field name, bounded by the item limit."""
    if len(mapping) > MAX_REDACT_ITEMS:
        return {"_summary": f"<dict keys={len(mapping)}>"}
    out: dict[str, object] = {}
    for key, value in mapping.items():
        name = str(key)
        out[name] = REDACTED if is_sensitive_field(name) else _redact_value(value, depth)
    return out


_MESSAGE_PAIR: Final = re.compile(
    r"""(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)=(?P<value>
        "[^"]*"                 # a double-quoted value, spaces and all
      | '[^']*'                 # a single-quoted value
      | [^,;\n]*                # otherwise: run to a separator or end of line
    )""",
    re.VERBOSE,
)
"""One ``key=value`` pair inside a rendered message string.

THE VALUE GRAMMAR IS THE WHOLE POINT, and round 1 got it wrong. It matched
``\\S+`` -- a single non-space run -- so ``subject=Ferraro matter mediation``
was rewritten to ``subject=[redacted] matter mediation`` and every word after
the first shipped to all three sinks. A multi-word subject or body is the
COMMON case, not an edge one, so the bug leaked essentially the whole value it
claimed to withhold.

The three alternatives are ordered, and the order is load-bearing: a quoted
value wins so an internal comma stays inside the value, and the bare form runs
to a separator (``,`` / ``;`` / end of line) rather than to a space, so
multi-word prose is consumed whole. ``_format_fields`` emits space-separated
pairs, so a bare value stops at the next ``name=`` -- see ``_redact_pair``,
which hands the trailing ``key=`` of a following pair back rather than eating
it.
"""

_NEXT_PAIR_START: Final = re.compile(r"\s(?=[A-Za-z_][A-Za-z0-9_.\-]*=)")
"""Where the NEXT pair begins inside a greedy bare value.

``_format_fields`` separates pairs with a space, so the bare branch of
``_MESSAGE_PAIR`` -- which deliberately runs past spaces to catch a multi-word
value -- swallows every following pair on the line. ``subject=Long Multi Word
phase=ok`` matches as ONE pair whose value is ``Long Multi Word phase=ok``.

Splitting there and re-scanning the remainder is what makes the two
requirements coexist: a multi-word value is redacted WHOLE, and the fields
after it are still examined on their own names rather than being hidden inside
somebody else's redacted value. Redaction that swallowed the rest of the line
would look identical in a passing leak test and destroy the diagnostics.
"""


def _redact_pair(match: re.Match[str]) -> str:
    """Rewrite one matched ``key=value`` pair, re-scanning any pairs it swallowed.

    Args:
        match: One ``_MESSAGE_PAIR`` match.

    Returns:
        The rewritten pair, with any following pairs the greedy value consumed
        redacted on their own names.
    """
    name = match.group("name")
    value = match.group("value")
    # A greedy bare value eats the pairs after it. Split at the next pair and
    # recurse on the remainder so each name is judged by its own rule.
    tail = ""
    if (following := _NEXT_PAIR_START.search(value)) is not None:
        tail = _redact_message(value[following.start() :])
        value = value[: following.start()]
    if not is_sensitive_field(name):
        return f"{name}={value}{tail}"
    return f"{name}={REDACTED}{tail}"


def _redact_message(message: str) -> str:
    """Redact ``key=value`` pairs whose NAME is sensitive, inside a message string.

    This is a DEFENSIVE BACKSTOP, not the mechanism the module relies on. It
    covers exactly one shape -- a ``name=value`` pair -- and it covers that
    shape for multi-word, quoted, and separator-delimited values. It does not
    and cannot cover free prose: there is no way to tell a mail body from a
    diagnostic sentence by inspection, so a rule that blanked unnamed text
    would blank real diagnostics and still miss content phrased as a sentence.

    That gap is why free-form content is kept OUT of the message string at the
    source instead of being cleaned out of it afterwards -- see
    ``find_content_interpolations``, which is the guard that actually holds the
    line. Unattributed prose that reaches here anyway is bounded by
    ``MAX_MESSAGE_CHARS`` in ``_redact``, so a pasted body is truncated to a
    length summary rather than stored whole.

    Args:
        message: The rendered log message.

    Returns:
        The message with sensitive ``key=value`` values replaced.
    """
    return _MESSAGE_PAIR.sub(_redact_pair, message)


# ---------------------------------------------------------------------------
# THE GUARD: content may not enter a message string in the first place
# ---------------------------------------------------------------------------

LOG_METHOD_NAMES: Final[frozenset[str]] = frozenset({
    "debug",
    "info",
    "warning",
    "error",
    "critical",
    "exception",
    "success",
    "trace",
    "log",
})
"""Loguru emit methods. A call to one of these is a logging call site."""

MESSAGE_BUILDER_NAMES: Final[frozenset[str]] = frozenset({"fallback", "guard", "retry"})
"""Wrapper methods whose first argument is folded into a message string.

``ProviderCall.fallback("reason", ...)`` and its siblings pass ``reason``
through ``_line`` into the rendered message, so a content variable there
reaches a sink exactly as an f-string would. They are guarded on the same rule.
"""


class ContentInterpolation(NamedTuple):
    """One place a content-bearing value is built into a log message string."""

    file: str
    line: int
    expression: str


def _interpolated_leaf(node: ast.expr) -> str | None:
    """The base NAME an interpolated expression reads from, if it has one.

    ``{body}`` -> ``body``; ``{message.subject}`` -> ``subject``;
    ``{len(body)}`` -> None, because a length is a count, not content.

    Args:
        node: The expression inside an f-string placeholder or format argument.

    Returns:
        The name to judge, or None when the expression cannot carry content.
    """
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=attr):
            return attr
        case ast.Subscript(value=inner):
            return _interpolated_leaf(inner)
        # A CALL is a derived value -- len(), type(exc).__name__, str.strip().
        # Judged by the call's own name, so `body.decode()` is still content
        # while `len(body)` is a count. A bare call with no name is not content.
        case ast.Call(func=ast.Attribute(attr=attr)) if attr not in {
            "join",
            "strip",
            "lower",
            "upper",
        }:
            return None if attr in ID_FIELD_SUFFIXES else attr
        case ast.Call():
            return None
        case _:
            return None


def _is_name_attached(parts: list[ast.expr], index: int) -> bool:
    """Whether the placeholder at ``index`` is preceded by a literal ``name=``.

    ``f"subject={subject}"`` is attached: the rendered text carries a name, so
    ``_redact_message`` can match it and blank the value. ``f"upstream said
    {subject}"`` is not, and nothing downstream can catch it -- which is why
    that shape is refused here instead.
    """
    if index == 0:
        return False
    previous = parts[index - 1]
    return isinstance(previous, ast.Constant) and str(previous.value).endswith("=")


LEVEL_FIRST_METHOD_NAMES: Final[frozenset[str]] = frozenset({"log"})
"""Emit methods whose FIRST argument is a level, not the message.

``logger.log("INFO", msg, *args)`` is the one loguru emitter that shifts the
message right by one. Reading argument 0 as the message there scans the string
``"INFO"`` -- always clean, always a pass -- and never looks at the argument
that actually renders, which is the worst possible outcome for a guard: it
reports on the wrong expression and calls the file safe.
"""


def _message_expressions(call: ast.Call) -> list[ast.expr]:
    """Every argument of ``call`` that can reach the rendered log MESSAGE.

    A LIST, not one expression, because loguru's own formatting API renders
    later arguments INTO the message: ``logger.info("upstream said {}", body)``
    puts ``body`` in the message text exactly as an f-string would. A guard
    that read only argument 0 saw a clean literal and reported nothing, so the
    structural rule this module rests on did not cover loguru's documented way
    of writing a log line -- the shape a new call site is most likely to use.

    Two argument roles therefore count, and both are returned:

    - the MESSAGE argument itself (position 0, or position 1 for
      ``logger.log``, whose first argument is the level);
    - every positional and keyword argument AFTER it, which loguru substitutes
      into the message's ``{}`` / ``{name}`` placeholders.

    The level argument of ``logger.log`` is deliberately excluded rather than
    scanned defensively: it is a level name or a variable holding one, never
    content, and including it would refuse ``logger.log(level, ...)``.

    Args:
        call: Any call node encountered while walking a module.

    Returns:
        The expressions to judge, empty when this is not a logging call.
    """
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name is None:
        return []
    if name in MESSAGE_BUILDER_NAMES:
        # ``fallback``/``guard``/``retry`` fold argument 0 into the message and
        # take the REST as bound **fields, which _format_fields redacts by name.
        return list(call.args[:1])
    if name not in LOG_METHOD_NAMES:
        return []
    message_index = 1 if name in LEVEL_FIRST_METHOD_NAMES else 0
    return [*call.args[message_index:], *(kw.value for kw in call.keywords)]


def _fstring_content_parts(values: list[ast.expr]) -> list[ast.expr]:
    """Interpolated parts of an f-string that no adjacent literal already names.

    Extracted so ``_scan_message_argument`` dispatches on node shape with no
    nested ``if``: "is this a placeholder" and "does a literal already label
    it" are one filter over one sequence, and a filter belongs beside the
    sequence it filters rather than inside a branch of the caller.
    """
    return [
        part.value
        for index, part in enumerate(values)
        if isinstance(part, ast.FormattedValue) and not _is_name_attached(values, index)
    ]


def _scan_message_argument(node: ast.expr, source: str) -> list[ContentInterpolation]:
    """Content-bearing interpolations inside one message argument."""
    found: list[ContentInterpolation] = []

    def record(expr: ast.expr) -> None:
        """Append ``expr`` to ``found`` when its interpolated leaf names content."""
        leaf = _interpolated_leaf(expr)
        # A MODULE CONSTANT is a bound, not content: `MAX_DRAFT_BODY_CHARS`
        # exists to be named in the message that explains the bound.
        if leaf is None or leaf.isupper() or not is_sensitive_field(leaf):
            return
        found.append(ContentInterpolation(source, expr.lineno, ast.unparse(expr)))

    # Dispatch on the SHAPE of the message argument. `match` rather than an
    # if/elif chain because every arm is a distinct AST node type, so each
    # pattern IS the structure it selects -- an arm cannot quietly stop
    # matching the way a hand-written isinstance conjunction can.
    match node:
        case ast.JoinedStr(values=values):
            for expr in _fstring_content_parts(values):
                record(expr)
        # `"...%s" % value` and `"...{}".format(value)` render identically to
        # an f-string, so they are the same defect wearing a different
        # spelling.
        case ast.BinOp(op=ast.Mod(), right=ast.Tuple(elts=operands)):
            for operand in operands:
                record(operand)
        case ast.BinOp(op=ast.Mod(), right=operand):
            record(operand)
        case ast.Call(func=ast.Attribute(attr="format"), args=args, keywords=keywords):
            for argument in [*args, *(kw.value for kw in keywords)]:
                record(argument)
        case _:
            # A BARE expression in message position. Either it IS the message
            # (``logger.info(reason)``) or loguru substitutes it into one
            # (``logger.info("said {}", body)``); both render the value into
            # the message text, so both are judged on the name they read from.
            # A literal is not a name and ``_interpolated_leaf`` returns None
            # for it, so the common ``logger.info("a sentence")`` is untouched.
            record(node)
    return found


def find_content_interpolations(package_root: Path) -> list[ContentInterpolation]:
    """Every logging call under ``package_root`` that builds content into its message.

    THIS IS THE MECHANISM THAT ACTUALLY HOLDS THE LINE, and it works by making
    the leak unrepresentable rather than by cleaning up after it. Content
    travels to a sink as a BOUND FIELD, where redaction matches on the field's
    name and is reliable. The message string is left literal-or-identifiers, so
    there is nothing in it for a redactor to have to parse.

    Enforced by AST, over the whole package, not per call site. That is the
    difference that matters: a rule applied at call sites is a rule a NEW call
    site can forget, and round 2 of this kit's review found exactly that failure
    -- guards added at the sites that were known, bypassed by a path that was
    not on the list. A walk over every ``*.py`` in the package cannot be
    forgotten by a file that did not exist when the rule was written.

    Three spellings are covered because they render identically: an f-string,
    ``%``-formatting, and ``str.format``. An interpolation with its NAME
    attached (``f"subject={subject}"``) is allowed -- ``_redact_message`` blanks
    that shape by name -- and a count or an id (``len(body)``, ``body_len``) is
    allowed because that is what the standard says to log instead of a value.

    Args:
        package_root: Directory of the package to scan.

    Returns:
        Every offending interpolation, empty when the package is clean.

    Raises:
        SyntaxError: If a source file cannot be parsed. Deliberately not caught:
            a guard that silently skips a file it could not read is a guard that
            reports clean on the file most likely to be wrong.
    """
    offenders: list[ContentInterpolation] = []
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for message in _message_expressions(node):
                offenders.extend(_scan_message_argument(message, path.name))
    return offenders


def _bounded(message: str) -> str:
    """Cap a redacted message, naming the length it dropped.

    A message past ``MAX_MESSAGE_CHARS`` is not a diagnostic sentence; it is
    content that got pasted somewhere. Truncating names the length so the line
    stays diagnosable without carrying the payload. Separate from ``_redact``
    so the bound is one named rule rather than a branch nested inside a type
    guard.
    """
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return f"{message[:MAX_MESSAGE_CHARS]}... <truncated, total len={len(message)}>"


# ruff: ignore[error-suffix-on-exception-name]  # named by the redaction contract; never raised
class RedactedException(Exception):
    """The stand-in ``_stripped_exception`` substitutes for a real exception's message."""


def _stripped_exception(exc: BaseException) -> BaseException:
    """Return a stand-in that keeps the TYPE and drops the MESSAGE.

    A traceback's last line renders ``str(exc)``, and an exception raised
    inside a provider call routinely interpolates the upstream response
    into that string. The frames are the diagnostic value; the message is
    where mail content rides. The patcher keeps the former and replaces
    the latter with the type name and the dropped length, so a reader
    knows what broke and how much was withheld.
    """
    return RedactedException(f"{type(exc).__name__}: <message redacted, len={len(str(exc))}>")


def _redact(record: Record) -> None:
    """loguru patcher: sanitize every record before any sink sees it.

    Runs on the record itself, so all three sinks receive the redacted form and
    none of them can be the one that leaks. Both halves of the record are swept
    -- the bound extras AND the message string -- because a sink renders the
    message, so redacting only the extras leaves the easier leak wide open.
    The attached exception, when there is one, keeps its frames and type but
    loses its message -- see ``_stripped_exception``.
    """
    message = record.get("message")
    if isinstance(message, str):
        record["message"] = _bounded(_redact_message(message))

    extra = record.get("extra")
    if isinstance(extra, dict):
        correlation_id = extra.get("correlation_id", DEFAULT_CORRELATION_ID)
        redacted = _redact_mapping(extra, 0)
        # correlation_id is an id by definition and must survive the sweep, or
        # the format string loses the key it interpolates.
        redacted["correlation_id"] = correlation_id
        record["extra"] = redacted

    exception = record.get("exception")
    if exception is not None and exception.value is not None:
        record["exception"] = exception._replace(value=_stripped_exception(exception.value))


def setup(settings: LoggingSettings, *, env: str) -> None:
    """Configure every logging sink for the process. Idempotent.

    Called once, by ``config.load_settings``. Removes existing sinks first, so
    a second call yields the same three sinks rather than six.

    Args:
        settings: Sink levels, rotation, retention, and which sinks are on.
        env: Used in log filenames, so two deployments never share a file.

    Raises:
        OSError: If the log directory cannot be created. Deliberately not
            caught -- a connector that cannot write its logs should fail at
            startup rather than run blind over a mailbox.
    """
    logger.remove()
    logger.configure(
        extra={"correlation_id": DEFAULT_CORRELATION_ID, "service": SERVICE_NAME},
        patcher=_redact,
    )

    # STDERR, NOT STDOUT. stdout carries the MCP JSON-RPC frames; a log line
    # there corrupts the frame the host AI is parsing and kills the transport.
    # This is not configurable, deliberately.
    logger.add(
        sys.stderr,
        # console_level is None on an install that names only `level`, so the
        # console keeps following it and nothing about that install changes.
        level=settings.console_level or settings.level,
        format=CONSOLE_FORMAT,
        colorize=True,
        backtrace=settings.verbose_tracebacks,
        # NEVER, on any sink: diagnose renders the local variable VALUES of
        # every traceback frame, so the body a provider call was holding is
        # printed beside the frame that raised. The patcher cannot reach it --
        # it strips the exception MESSAGE, while this is rendered by loguru
        # from the frame objects afterwards. stderr is redirected to a file by
        # any supervisor, so "console only" is not a containment boundary.
        diagnose=False,
    )

    if not settings.file_sink:
        return

    log_dir = Path(settings.directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    _add_file_sink(settings, log_dir / f"{SERVICE_NAME}-{env}.log")
    if settings.json_sink:
        _add_json_sink(settings, log_dir / f"{SERVICE_NAME}-{env}.jsonl")

    logger.info(
        f"LOG: sinks ready level={settings.level} "
        f"console={settings.console_level or settings.level} "
        f"file={settings.file_sink} "
        f"json={settings.json_sink} dir={settings.directory}"
    )


def _add_file_sink(settings: LoggingSettings, path: Path) -> None:
    """Plain-text rotating sink -- the one a human tails."""
    logger.add(
        path,
        level=settings.level,
        format=FILE_FORMAT,
        rotation=settings.rotation,
        retention=settings.retention,
        enqueue=True,
        compression="gz",
        backtrace=settings.verbose_tracebacks,
        # NEVER on disk: diagnose renders local variable VALUES into the
        # traceback, and a frame in this package holds mail bodies.
        diagnose=False,
    )


def _add_json_sink(settings: LoggingSettings, path: Path) -> None:
    """Structured sink -- one JSON object per line, for aggregation."""
    logger.add(
        path,
        level=settings.level,
        serialize=True,
        rotation=settings.rotation,
        retention=settings.retention,
        enqueue=True,
        compression="gz",
        diagnose=False,
    )


def new_correlation_id() -> str:
    """A short id tying one operation's five log points together."""
    return uuid.uuid4().hex[:12]


def _format_fields(fields: dict[str, object]) -> str:
    """Render ``key=value`` pairs, redacting any value whose NAME is sensitive.

    This is the FIRST of two passes over the same rule. It redacts as the line
    is built; ``_redact`` then re-checks the finished message string on the
    record, so a message assembled anywhere else -- an f-string at a call site,
    not this helper -- gets the identical treatment. Redacting here as well
    keeps the value out of the string in the first place rather than relying on
    the patcher to take it back out.
    """
    parts = [
        f"{name}={REDACTED if is_sensitive_field(name) else value}"
        for name, value in fields.items()
    ]
    return " ".join(parts)


def _is_operator_refusal(exc: BaseException) -> bool:
    """Is this an operator-facing REFUSAL rather than a call that broke?

    Keyed on the shared base, not on a concrete class or a tuple of them. It
    used to import ``config.ConfigError`` and test against that alone, which
    made the exemption something every new sibling refusal had to be
    remembered into -- and ``AuthError`` was not, so a fresh unauthenticated
    install printed two full provider tracebacks under the one-line contract
    (#195). ``isinstance`` against ``OperatorRefusalError`` answers for every
    refusal the moment it is declared.

    ``errors`` imports nothing, so this is a plain module-level import with no
    cycle to defer around: ``config`` imports THIS module at its own top level,
    which is why the old ``ConfigError`` lookup had to be deferred and wrapped
    in an ``ImportError`` guard. Reaching a base that stands alone removes both.
    """
    return isinstance(exc, OperatorRefusalError)


class _FivePointCall:
    """Shared entry/exit/failure/fallback/guard emitter. Not used directly.

    Subclasses supply the log prefix and the operation label. The context
    manager emits entry on ``__enter__``, exit-with-duration on a clean
    ``__exit__``, and failure with ``opt(exception=True)`` on an exception -- then
    re-raises, because swallowing is the one thing the standard forbids
    outright.
    """

    prefix: str = "CALL"

    def __init__(self, operation: str, **fields: object) -> None:
        """Record the operation and its identifying context.

        Args:
            operation: Stable event name, not a sentence.
            **fields: Identifiers and counts only. A value whose field name
                looks like content is redacted before it reaches a sink.
        """
        self.operation = operation
        self.fields = fields
        self.correlation_id = new_correlation_id()
        self._started = 0.0
        self._context: Any = None

    def _line(self, extra: dict[str, object] | None = None) -> str:
        """Render one log line: prefix, operation, then the redacted fields.

        Every one of the five points goes through here, so redaction by field
        name (``_format_fields``) happens once at the single place a line is
        built rather than at each emitting site, where one omission would leak.

        Args:
            extra: Per-point fields (``phase``, duration, guard detail) merged
                over the call's standing fields for this line only.
        """
        merged = {**self.fields, **(extra or {})}
        rendered = _format_fields(merged)
        return f"{self.prefix}: {self.operation} {rendered}".rstrip()

    def __enter__(self) -> Self:
        """Bind the correlation id and emit the ENTRY point (debug).

        Returns ``Self``, not the base class, so a ``ProviderCall`` used as a
        context manager stays a ``ProviderCall`` to the type checker and can be
        handed to ``providers.retry``.
        """
        self._context = logger.contextualize(correlation_id=self.correlation_id)
        self._context.__enter__()
        self._started = time.monotonic()
        logger.debug(self._line({"phase": "start"}))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> Literal[False]:
        """Emit the EXIT (info) or FAILURE (error) point, then unbind.

        The return type is ``Literal[False]``, not ``bool``, and that is
        load-bearing twice over. It tells the type checker the block never
        suppresses -- so a ``return`` inside a wrapped body is reachable rather
        than being reported as dead code -- and it states in the signature that
        this wrapper OBSERVES failures and never swallows one, which is the
        rule the observability standard is most emphatic about.

        Returns:
            False, always.
        """
        elapsed_ms = int((time.monotonic() - self._started) * 1000)
        if exc is None:
            logger.info(self._line({"phase": "ok", "ms": elapsed_ms}))
        else:
            line = self._line({
                "phase": "failed",
                "ms": elapsed_ms,
                "error_type": type(exc).__name__,
            })
            # ERROR always carries the exception (loguru ``opt(exception=True)``;
            # stdlib's exc_info spelling is silently dropped). A failure line without the
            # traceback is a line that says something broke and not what.
            #
            # An ``OperatorRefusalError`` is the ONE exception to that (#195). It is
            # not a call that broke -- it is an operator-facing REFUSAL the CLI
            # renders as one line and returns 1 on, and the wrapper sits inside
            # that refusal's re-raise path. Dumping a traceback here puts a stack
            # over the top of the sentence the operator is supposed to read: an
            # uninitialized install printed 48 stderr lines for a one-line
            # contract, and an unauthenticated one printed two more from the
            # provider. The failure is still recorded at ERROR with its
            # ``error_type``, so the observability standard's "never swallow"
            # rule holds; only the traceback is withheld.
            if _is_operator_refusal(exc):
                logger.error(line)
            else:
                logger.opt(exception=True).error(line)
        if self._context is not None:
            self._context.__exit__(exc_type, exc, tb)
        return False

    def retry(self, attempt: int, **fields: object) -> None:
        """Emit the RETRY point (warning). One line per attempt, with its number."""
        logger.warning(self._line({"phase": "retry", "attempt": attempt, **fields}))

    def fallback(self, reason: str, **fields: object) -> None:
        """Emit the FALLBACK/degraded point (warning) with the concrete impact."""
        logger.warning(self._line({"phase": "fallback", "reason": reason, **fields}))

    def guard(self, reason: str, **fields: object) -> None:
        """Emit the GUARD/limit point (warning) when a bound clamps a request."""
        logger.warning(self._line({"phase": "guard", "reason": reason, **fields}))


class ProviderCall(_FivePointCall):
    """Five-point wrapper around ONE outbound provider API call.

    Wraps the network boundary, which is where the standard says to log: a
    DEBUG line per I/O call, not per statement.

    Example:
        >>> with ProviderCall("google", "list_inbox", limit=50) as call:
        ...     call.guard("limit clamped", requested=500, applied=200)
    """

    prefix = "PROVIDER"

    def __init__(self, provider: str, operation: str, **fields: object) -> None:
        """Record which provider, which operation, and the identifying context."""
        super().__init__(operation, provider=provider, **fields)


class ToolCall(_FivePointCall):
    """Five-point wrapper around ONE MCP tool invocation.

    The correlation id bound here also covers every ``ProviderCall`` made
    inside the block, so one tool invocation reads as one story in the log.
    """

    prefix = "TOOL"


def install_crash_handler() -> None:
    """Log an unhandled exception before the process dies.

    An unlogged crash leaves nothing to diagnose (the observability standard's
    crash-visibility rule). ``KeyboardInterrupt`` is passed through untouched:
    a Ctrl-C is an operator decision, not a defect, and logging it at CRITICAL
    trains people to ignore the level.
    """
    previous = sys.excepthook

    def handler(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: types.TracebackType | None,
    ) -> None:
        """Emit CRITICAL with the traceback, then hand off to the previous hook.

        The prior ``sys.excepthook`` is always called last so the interpreter's
        own stderr traceback and exit status are unchanged - this hook adds a
        log record, it does not take over crash handling.
        """
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        logger.opt(exception=(exc_type, exc, tb)).critical(
            f"CRASH: unhandled {exc_type.__name__} -- the connector is exiting"
        )
        previous(exc_type, exc, tb)

    sys.excepthook = handler
