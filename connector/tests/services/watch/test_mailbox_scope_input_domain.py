"""INPUT-DOMAIN CHARACTERIZATION of ``MailboxScope``.

Split out of ``test_mailbox_scope.py`` on 2026-08-19 (500-code-line ceiling) at
that file's own ``===`` divider. Its sibling asserts the CONTRACT: the behaviour
the docstrings promise. This file characterizes the INPUT DOMAIN -- every
partition of every public parameter, including the ones no caller was expected
to reach: wrong types, empty and whitespace names, unknown tags, unicode,
separators in awkward positions, duplicates, and contradictory combinations.

TWO KINDS OF TEST LIVE HERE, and the difference matters.

  * ``test_*_is_defined`` rows assert behaviour that is CORRECT and simply had
    no coverage before. They are contract tests like any other.

  * ``test_*_documents_current_behaviour`` rows record what the code does today
    on an input nobody designed for. Where the current answer is WRONG they are
    ``xfail(strict=True)``, which asserts the bug is still present: the run goes
    green while the defect exists and turns RED the moment somebody fixes it, so
    the fix cannot land silently and the expectation written here is the CORRECT
    one, not the observed one.

Nothing below quietly asserts "whatever the code happens to do" as intended.
The full matrix, including the six fail-open findings, is in the lane report.
"""

from __future__ import annotations

import pytest

# The named scopes and the config surface come from the harness so the two
# halves of this split cannot drift into probing differently-constructed scopes
# that share a name.
from _mailbox_scope_harness import (
    ALL_FOLDERS,
    EXCEPT_MATTERS,
    INBOX_ONLY,
    NESTED_EXCLUDE,
    MailboxScope,
    ValidationError,
    default_scope,
)

# ===========================================================================
# INPUT-DOMAIN CHARACTERIZATION
#
# The tables above assert the CONTRACT: the behaviour the docstrings promise.
# The tables below characterize the INPUT DOMAIN -- every partition of every
# public parameter, including the ones no caller was expected to reach: wrong
# types, empty and whitespace names, unknown tags, unicode, separators in
# awkward positions, duplicates, and contradictory combinations.
#
# TWO KINDS OF TEST LIVE HERE, and the difference matters.
#
#   * `test_*_is_defined` rows assert behaviour that is CORRECT and simply had
#     no coverage before. They are contract tests like any other.
#
#   * `test_*_documents_current_behaviour` rows record what the code does today
#     on an input nobody designed for. Where the current answer is WRONG they
#     are `xfail(strict=True)`, which asserts the bug is still present: the run
#     goes green while the defect exists and turns RED the moment somebody
#     fixes it, so the fix cannot land silently and the expectation written
#     here is the CORRECT one, not the observed one.
#
# Nothing below quietly asserts "whatever the code happens to do" as intended.
# The full matrix, including the six fail-open findings, is in the lane report.
# ===========================================================================


# ---------------------------------------------------------------------------
# Wrong-type inputs -- the whole class was unexercised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected_error"),
    [
        pytest.param(lambda: MailboxScope().covers(None), AttributeError, id="covers-None"),
        pytest.param(lambda: MailboxScope().covers(123), AttributeError, id="covers-int"),
        pytest.param(lambda: MailboxScope().covers(b"Billing"), AttributeError, id="covers-bytes"),
        pytest.param(
            lambda: ALL_FOLDERS.covers("X", well_known=5), AttributeError, id="well_known-int"
        ),
        pytest.param(
            lambda: EXCEPT_MATTERS.covers_all(["Billing", None]),
            AttributeError,
            id="covers_all-None-element",
        ),
        pytest.param(
            lambda: EXCEPT_MATTERS.covers_all(["Billing", 7]),
            AttributeError,
            id="covers_all-int-element",
        ),
        pytest.param(
            lambda: EXCEPT_MATTERS.covers_all(x for x in ["Billing"]),
            TypeError,
            id="covers_all-generator-has-no-len",
        ),
        pytest.param(
            lambda: EXCEPT_MATTERS.covers_all(["Billing"], well_known=None),
            TypeError,
            id="covers_all-well_known-None",
        ),
    ],
)
def test_a_non_string_input_raises_rather_than_answering(
    call: object, expected_error: type[Exception]
) -> None:
    """Wrong types must NOT resolve to a permission answer.

    The important property is that no partition here returns ``True``. The
    exception TYPE is incidental -- these surface a raw ``AttributeError`` from
    ``.strip()`` rather than a typed error, which is a rough edge recorded in
    the lane report, not a contract worth pinning to an exact message.
    """
    with pytest.raises(expected_error):
        call()  # type: ignore[operator]


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        pytest.param({"mode": None}, "mode", id="mode-None"),
        pytest.param({"mode": ""}, "mode", id="mode-empty"),
        # The Literal is exact, so a differently-cased mode is a typo not a value.
        pytest.param({"mode": "ALL_FOLDERS"}, "mode", id="mode-wrong-case"),
        pytest.param(
            {"mode": "all_except", "excluded_folders": "AB"}, "excluded_folders", id="list-bare-str"
        ),
        pytest.param(
            {"mode": "all_except", "excluded_folders": (None,)},
            "excluded_folders",
            id="list-None-element",
        ),
        pytest.param(
            {"mode": "all_except", "excluded_folders": (5,)},
            "excluded_folders",
            id="list-int-element",
        ),
        pytest.param(
            {"mode": "all_except", "excluded_folders": ("\t\n",)},
            "excluded_folders",
            id="list-tab-newline-only",
        ),
    ],
)
def test_malformed_construction_input_is_refused(kwargs: dict[str, object], field: str) -> None:
    """A bad field value is a startup error naming the field, never a default.

    ``excluded_folders="AB"`` is the notable row: a bare string is REFUSED here
    rather than silently becoming the characters ``("A","B")``. ``covers_all``
    does not make that same check, which is finding F1 in the lane report.
    """
    with pytest.raises(ValidationError, match=field):
        MailboxScope(**kwargs)  # type: ignore[arg-type]


def test_a_folder_list_accepts_a_list_and_normalises_to_a_tuple() -> None:
    """The model is declared with tuples; a list is the obvious caller mistake."""
    scope = MailboxScope(mode="all_except", excluded_folders=["Client Matters"])  # type: ignore[arg-type]
    assert scope.excluded_folders == ("Client Matters",)
    assert isinstance(scope.excluded_folders, tuple)
    assert scope.covers("Client Matters") is False


# ---------------------------------------------------------------------------
# default_scope() -- the full input domain of a one-argument factory
# ---------------------------------------------------------------------------


def test_default_scope_is_keyword_only_and_returns_independent_objects() -> None:
    """A frozen model returned by a factory must not be a shared singleton.

    ``names_encode_nesting`` is keyword-only on purpose: a bare positional bool
    at a call site reads as nothing at all, and this is the field that decides
    whether an exclusion reaches a privileged child.
    """
    assert default_scope() == default_scope()
    assert default_scope() is not default_scope()
    assert default_scope(names_encode_nesting=True).names_encode_nesting is True
    assert default_scope(names_encode_nesting=False).names_encode_nesting is False
    with pytest.raises(TypeError):
        default_scope(True)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# well_known -- tag normalisation that IS defined
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "folder", "well_known", "expected"),
    [
        pytest.param(INBOX_ONLY, "Zzz", "INBOX", True, id="tag-uppercase-normalised"),
        pytest.param(INBOX_ONLY, "Zzz", " inbox ", True, id="tag-padded-normalised"),
        pytest.param(ALL_FOLDERS, "Deleted Items", None, False, id="tag-None-uses-display-name"),
        pytest.param(ALL_FOLDERS, "Deleted Items", "", False, id="tag-empty-falls-back-to-name"),
    ],
)
def test_a_well_known_tag_is_normalised_like_a_folder_name(
    scope: MailboxScope, folder: str, well_known: str | None, expected: bool
) -> None:
    """Tags are stripped and casefolded, so a caller's spelling cannot matter."""
    assert scope.covers(folder, well_known=well_known) is expected


# ---------------------------------------------------------------------------
# Unicode -- no non-ASCII folder name appeared anywhere in the old suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "probe", "expected_covered"),
    [
        pytest.param("Kanzlei", "kanzlei", False, id="non-ascii-casefold-matches"),
        pytest.param("Mandanten", "MANDANTEN", False, id="non-ascii-uppercase-matches"),
        pytest.param("\U0001f4c1Legal", "\U0001f4c1legal", False, id="emoji-name-matches"),
        # casefold() is not locale-aware: Turkish dotless i does NOT fold to
        # ASCII i, so these are genuinely different names and stay covered.
        # Written as escapes: the literal glyph trips ruff's ambiguous-unicode
        # rule, and the point of the row is precisely that it is NOT ASCII "i".
        pytest.param("BILLING", "b\u0131ll\u0131ng", True, id="dotless-i-is-a-different-name"),
    ],
)
def test_unicode_folder_names_fold_by_codepoint(
    configured: str, probe: str, expected_covered: bool
) -> None:
    """Exclusion matching on non-ASCII names, which nothing previously covered."""
    scope = MailboxScope(mode="all_except", excluded_folders=(configured,))
    assert scope.covers(probe) is expected_covered


# ---------------------------------------------------------------------------
# Duplicates, and lists that the mode does not consult
# ---------------------------------------------------------------------------


def test_duplicate_folder_names_are_kept_verbatim_and_count_toward_the_cap() -> None:
    """Documents a real weakening of ``MAX_SCOPE_FOLDERS`` as a human-review proxy.

    The cap exists so a list "that long was not read by the human who is
    accountable for it". Duplicates are not collapsed, so 200 copies of one
    folder name pass the cap while naming exactly one folder. Matching is
    unaffected, which is why this is recorded rather than treated as a defect.
    """
    scope = MailboxScope(mode="all_except", excluded_folders=("Clients",) * 5)
    assert scope.excluded_folders == ("Clients",) * 5
    assert scope.covers("Clients") is False
    # Differing case and differing whitespace both survive as separate entries.
    assert MailboxScope(
        mode="all_except", excluded_folders=("Clients", "clients")
    ).excluded_folders == ("Clients", "clients")
    assert MailboxScope(
        mode="all_except", excluded_folders=("Clients", "  Clients  ")
    ).excluded_folders == ("Clients", "Clients")


def test_a_folder_list_the_mode_does_not_consult_is_inert() -> None:
    """Lists are mode-scoped, so a leftover list must not leak into another mode.

    ``all_folders`` ignores ``excluded_folders`` entirely -- a caller that sets
    a mode and forgets to clear the other list gets the mode it asked for, not
    a silent hybrid.
    """
    assert MailboxScope(mode="all_folders", excluded_folders=("Clients",)).covers("Clients") is True
    assert MailboxScope(mode="inbox_only", included_folders=("Clients",)).covers("Clients") is False


def test_the_overlap_validator_runs_regardless_of_mode() -> None:
    """A contradiction is refused even in a mode that reads only one list.

    The lists outlive the mode in a config file, so a contradiction left behind
    by an earlier answer must be caught when it is written, not when the mode
    later changes and starts consulting it.
    """
    with pytest.raises(ValidationError, match="both lists"):
        MailboxScope(mode="all_except", excluded_folders=("A",), included_folders=("A",))
    # Stripping happens before the comparison, so whitespace cannot smuggle one past.
    with pytest.raises(ValidationError, match="both lists"):
        MailboxScope(
            mode="only_listed", included_folders=("Billing",), excluded_folders=("  Billing  ",)
        )


# ---------------------------------------------------------------------------
# Separator placement -- with nesting ON, where "/" is load-bearing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        pytest.param("/Clients", True, id="leading-separator-is-not-a-child"),
        pytest.param("Clients/", False, id="trailing-separator-probe-matches-prefix"),
        pytest.param("Clients//Smith", False, id="double-separator-still-under-parent"),
        pytest.param("/", True, id="bare-separator-is-an-unrelated-name"),
    ],
)
def test_separator_placement_in_a_probed_name(folder: str, expected: bool) -> None:
    """Awkward separator positions in the name being ASKED about.

    All four are defensible: the boundary rule keys on the configured parent
    plus one separator, so anything that does not start with exactly that is
    not a child.
    """
    assert NESTED_EXCLUDE.covers(folder) is expected


def test_covers_all_does_not_mutate_the_sequences_it_is_given() -> None:
    """A scope question must be free of side effects on the caller's data.

    ``covers_all`` pads and slices the tag sequence internally; doing that in
    place would corrupt a label list the caller reuses for the next message.
    """
    names = ["Billing", "Client Matters"]
    tags: list[str | None] = ["inbox"]
    names_before, tags_before = list(names), list(tags)

    assert EXCEPT_MATTERS.covers_all(names, well_known=tags) is False

    assert names == names_before
    assert tags == tags_before


def test_a_longer_well_known_sequence_is_truncated_rather_than_overrunning() -> None:
    """More tags than names must not raise, and must not shift the pairing."""
    assert ALL_FOLDERS.covers_all(["Billing"], well_known=["deleted", "junk"]) is False
    assert ALL_FOLDERS.covers_all(["Billing"], well_known=[None, "deleted"]) is True


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        pytest.param(None, False, id="names-None-fails-closed"),
        pytest.param(set(), False, id="names-empty-set-fails-closed"),
        pytest.param({"Billing"}, True, id="names-set-is-accepted"),
        pytest.param(["Billing", "Billing"], True, id="names-duplicate-benign"),
        pytest.param(["Client Matters", "Client Matters"], False, id="names-duplicate-excluded"),
    ],
)
def test_covers_all_container_shapes(names: object, expected: bool) -> None:
    """Shapes other than a list, including the falsy ones, must fail CLOSED."""
    assert EXCEPT_MATTERS.covers_all(names) is expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FAIL-OPEN FINDINGS
#
# Each of these currently answers "the watcher MAY read this" for a folder the
# configuration puts out of bounds. They are xfail(strict=True) asserting the
# CORRECT answer, so the suite is green while the defect stands and goes RED
# the moment one is fixed -- the fix then flips the marker instead of editing
# an expectation, and cannot land unnoticed.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="F1: a bare str is a Sequence[str] of CHARACTERS, so covers_all "
    "iterates per character, every character is in scope, and the exclusion is "
    "bypassed. Fix: reject str explicitly in covers_all.",
)
def test_a_bare_string_passed_as_names_must_not_bypass_an_exclusion() -> None:
    """THE WORST ONE. A caller passing one label unwrapped inverts the answer."""
    assert EXCEPT_MATTERS.covers_all("Client Matters") is False


@pytest.mark.xfail(
    strict=True,
    reason="F3: an unrecognised but truthy tag replaces the display-name lookup "
    "with a value matching no guard, so the system-folder check is skipped "
    "entirely. Fix: reject or ignore an unknown tag.",
)
@pytest.mark.parametrize("tag", ["bogus", "  "])
def test_an_unknown_well_known_tag_must_not_skip_the_system_folder_guard(tag: str) -> None:
    """include_deleted is False, so Deleted Items must stay refused."""
    assert ALL_FOLDERS.covers("Deleted Items", well_known=tag) is False


def test_an_inbox_tag_does_not_override_the_list_driven_modes() -> None:
    """The tag shortcut applies to the SYSTEM folders only, not to the lists.

    Worth pinning because the opposite is easy to assume: ``"inbox"`` is not
    one of the three guarded tags, so it falls through to the mode and the
    exclusion/inclusion list still decides. Only ``inbox_only`` reads the tag
    as an answer, which is what that mode means.
    """
    excluded = MailboxScope(mode="all_except", excluded_folders=("Client Matters",))
    assert excluded.covers("Client Matters", well_known="inbox") is False
    unlisted = MailboxScope(mode="only_listed", included_folders=("Billing",))
    assert unlisted.covers("Client Matters", well_known="inbox") is False
    assert MailboxScope().covers("Client Matters", well_known="inbox") is True


@pytest.mark.xfail(
    strict=True,
    reason="F4: a caller-supplied tag REPLACES the display-name lookup, so "
    "tagging Junk as 'inbox' means the junk guard never sees it and "
    "include_junk=False is bypassed. Fix: consult the system guards on the "
    "display name even when a tag is supplied.",
)
def test_an_inbox_tag_must_not_promote_a_junk_folder() -> None:
    """include_junk is False, so Junk must stay refused however it is tagged."""
    assert MailboxScope(include_junk=False).covers("Junk Email", well_known="inbox") is False


@pytest.mark.xfail(
    strict=True,
    reason="F5: a configured name already ending in the separator builds the "
    "prefix 'Clients//', so the real child never matches. Fix: strip a trailing "
    "separator from configured names before building the prefix.",
)
def test_a_configured_name_with_a_trailing_separator_still_governs_its_subtree() -> None:
    """The client wrote 'Clients/' to mean the subtree and got the opposite."""
    scope = MailboxScope(
        mode="all_except", excluded_folders=("Clients/",), names_encode_nesting=True
    )
    assert scope.covers("Clients/Smith") is False


@pytest.mark.xfail(
    strict=True,
    reason="F10: the overlap validator compares literal strings, so an "
    "inclusion of a parent and an exclusion of its child pass validation, and "
    "the inclusion cascade then wins. Fix: resolve nesting in the validator.",
)
def test_an_excluded_child_must_beat_an_included_parent() -> None:
    """An explicitly excluded label must not be read because its parent was included."""
    scope = MailboxScope(
        mode="only_listed",
        included_folders=("Clients",),
        excluded_folders=("Clients/Smith",),
        names_encode_nesting=True,
    )
    assert scope.covers("Clients/Smith") is False


@pytest.mark.xfail(
    strict=True,
    reason="F2: the query side has no blank-name guard, so a provider "
    "returning an empty display name is admitted by the wide modes. The config "
    "side already refuses blank names. Fix: refuse a blank probe name.",
)
@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("scope", [ALL_FOLDERS, EXCEPT_MATTERS], ids=["all_folders", "all_except"])
def test_a_blank_folder_name_must_not_be_covered(scope: MailboxScope, blank: str) -> None:
    """A nameless folder is a folder we know nothing about -- fail closed."""
    assert scope.covers(blank) is False
