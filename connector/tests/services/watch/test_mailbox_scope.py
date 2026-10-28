"""Which folders the watcher may read -- the CONTRACT the docstrings promise.

``MailboxScope.covers`` is a PURE function of the installed fields, so these
tests are the whole story about how a folder is admitted or refused. No model
call, no provider call, and nothing a message body can reach -- the client
answered once at install time and a script honours it forever.

The failure this file exists to prevent is silent: a scope rule that misses
because of capitalisation, or an empty exclusion list that quietly reads as
"everything", puts privileged mail into a summary with no error anywhere.

SHAPE OF THIS FILE. Scope resolution is a nearly-pure function of (mode, lists,
flags, folder name), so the cases are a TABLE and they are written as one. A
missing case is then a missing ROW -- visible in the table it belongs to --
rather than a missing file nobody notices is absent. Each ``id`` names the case,
so a failure still says which row broke without reading the parameters.

Every table below was proven RED against a targeted mutation of the source:
inverting any-exclusion-wins in ``covers_all``, changing ``NESTING_SEPARATOR``,
returning True unconditionally from ``covers``, an off-by-one on
``MAX_SCOPE_FOLDERS``, disabling the both-lists validator, and flipping the
``names_encode_nesting`` default. The mutation table is in the lane report.

SCOPE OF THIS FILE after the 2026-08-19 split (500-code-line ceiling): the
CONTRACT half -- installed defaults, ``covers()``, nested containers,
``covers_all()``, construction validation, the installer-payload round trip, the
per-provider keying, and the per-provider nesting default. The other half, which
characterizes the INPUT DOMAIN including the six fail-open findings, is
``test_mailbox_scope_input_domain.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The named scopes and the config surface come from the harness so the two
# halves of this split cannot drift into probing differently-constructed scopes
# that share a name.
from _mailbox_scope_harness import (
    ALL_FOLDERS,
    DEFAULT_SCOPES,
    EXCEPT_MATTERS,
    FLAT_EXCLUDE,
    FLAT_INCLUDE,
    GOOGLE,
    INBOX_ONLY,
    MAX_SCOPE_FOLDERS,
    MICROSOFT,
    NESTED_EXCLUDE,
    NESTED_INCLUDE,
    NESTING_SEPARATOR,
    ONLY_BILLING,
    PROVIDER_NAMES,
    SCOPE_MODES,
    ConnectorConfig,
    MailboxScope,
    ValidationError,
    default_config,
    default_scope,
    load_config,
    write_config,
)

# ---------------------------------------------------------------------------
# Defaults -- an upgrade must not widen what an existing deployment reads
# ---------------------------------------------------------------------------


def test_the_installed_defaults_are_the_narrowest_posture() -> None:
    """Every default that governs exposure, asserted together.

    These belong in one test because they are one claim: an install that never
    heard of scope reads Inbox and nothing else. Splitting them lets a widened
    default pass the file while failing the promise.
    """
    scope = MailboxScope()
    assert scope.mode == "inbox_only"
    assert scope.excluded_folders == ()
    assert scope.included_folders == ()
    assert scope.include_junk is False
    assert scope.include_deleted is False
    assert scope.include_archive is False
    assert scope.names_encode_nesting is False
    assert default_scope() == scope
    assert SCOPE_MODES == ("inbox_only", "all_folders", "all_except", "only_listed")
    assert NESTING_SEPARATOR == "/"


@pytest.mark.parametrize("provider", PROVIDER_NAMES)
def test_the_default_config_carries_the_narrowest_scope(provider: str) -> None:
    scope = default_config().mailbox_scope(provider)
    assert scope.mode == "inbox_only"
    assert scope.excluded_folders == ()
    assert scope.included_folders == ()
    assert (scope.include_junk, scope.include_deleted, scope.include_archive) == (
        False,
        False,
        False,
    )


# ---------------------------------------------------------------------------
# covers() -- the four modes, case folding, and the well-known system folders
#
# One table. Each row is (scope, folder name, well_known tag, expected answer),
# which is exactly the signature of the question a watcher asks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "folder", "well_known", "expected"),
    [
        # -- mode dispatch ---------------------------------------------------
        pytest.param(INBOX_ONLY, "Inbox", None, True, id="inbox_only-covers-inbox"),
        pytest.param(INBOX_ONLY, "Client Matters", None, False, id="inbox_only-refuses-other"),
        pytest.param(INBOX_ONLY, "Sent Items", None, False, id="inbox_only-refuses-sent"),
        pytest.param(INBOX_ONLY, "Posteingang", "inbox", True, id="inbox_only-localised-tag"),
        pytest.param(ALL_FOLDERS, "Client Matters", None, True, id="all_folders-covers-any"),
        pytest.param(EXCEPT_MATTERS, "Client Matters", None, False, id="all_except-refuses-named"),
        pytest.param(EXCEPT_MATTERS, "Newsletters", None, True, id="all_except-covers-unnamed"),
        pytest.param(ONLY_BILLING, "Billing", None, True, id="only_listed-covers-listed"),
        pytest.param(ONLY_BILLING, "Client Matters", None, False, id="only_listed-refuses-other"),
        # Inbox has no implicit privilege: "only these" means only these.
        pytest.param(ONLY_BILLING, "Inbox", None, False, id="only_listed-refuses-inbox"),
        # -- case folding and whitespace -------------------------------------
        # A client writes "Client Matters"; the mailbox spells it differently.
        # A case-sensitive miss here READS the folder -- silent exposure.
        pytest.param(EXCEPT_MATTERS, "client matters", None, False, id="exclusion-lowercase"),
        pytest.param(EXCEPT_MATTERS, "CLIENT MATTERS", None, False, id="exclusion-uppercase"),
        pytest.param(
            MailboxScope(mode="only_listed", included_folders=("billing",)),
            "Billing",
            None,
            True,
            id="inclusion-case-differs",
        ),
        pytest.param(
            EXCEPT_MATTERS, "  Client Matters  ", None, False, id="exclusion-surrounding-space"
        ),
        # Mixed case on purpose: an all-lowercase probe passes without casefolding.
        pytest.param(ALL_FOLDERS, "DELETED Items", None, False, id="system-name-mixed-case"),
        pytest.param(
            MailboxScope(mode="all_folders", include_deleted=True),
            "DELETED Items",
            None,
            True,
            id="system-name-mixed-case-admitted",
        ),
        # -- well-known system folders, decided BEFORE the mode ---------------
        pytest.param(ALL_FOLDERS, "Junk Email", None, False, id="all_folders-excludes-junk"),
        pytest.param(ALL_FOLDERS, "Deleted Items", None, False, id="all_folders-excludes-deleted"),
        pytest.param(ALL_FOLDERS, "Archive", None, False, id="all_folders-excludes-archive"),
        pytest.param(ALL_FOLDERS, "Spam", None, False, id="gmail-spelling-spam"),
        pytest.param(ALL_FOLDERS, "Trash", None, False, id="gmail-spelling-trash"),
        pytest.param(
            MailboxScope(mode="all_folders", include_junk=True),
            "Junk Email",
            None,
            True,
            id="include_junk-admits-junk",
        ),
        pytest.param(
            MailboxScope(mode="all_folders", include_deleted=True),
            "Deleted Items",
            None,
            True,
            id="include_deleted-admits-deleted",
        ),
        pytest.param(
            MailboxScope(mode="all_folders", include_archive=True),
            "Archive",
            None,
            True,
            id="include_archive-admits-archive",
        ),
        # A localised mailbox spells Deleted Items in the owner's language, so
        # the explicit tag has to beat the display-name table.
        pytest.param(
            ALL_FOLDERS, "Geloeschte Elemente", "deleted", False, id="tag-beats-localised-name"
        ),
        pytest.param(
            MailboxScope(mode="all_folders", include_deleted=True),
            "Geloeschte Elemente",
            "deleted",
            True,
            id="tag-beats-localised-name-admitted",
        ),
    ],
)
def test_covers_answers_one_folder(
    scope: MailboxScope, folder: str, well_known: str | None, expected: bool
) -> None:
    assert scope.covers(folder, well_known=well_known) is expected


def test_the_system_booleans_are_independent_and_outrank_the_mode() -> None:
    """Three booleans, three folders, and the mode that must not overrule them.

    Asserted together because independence is only observable as a
    COMBINATION: junk admitted while deleted and archive stay refused is the
    claim, and one assertion cannot state it.
    """
    junk_only = MailboxScope(mode="all_folders", include_junk=True)
    assert junk_only.covers("Junk Email") is True
    assert junk_only.covers("Deleted Items") is False
    assert junk_only.covers("Archive") is False

    # Decided before the mode, so the boolean holds even at the narrowest one.
    narrow = MailboxScope(include_junk=True)
    assert narrow.covers("Junk Email") is True
    assert narrow.covers("Client Matters") is False


# ---------------------------------------------------------------------------
# NESTED CONTAINERS -- Gmail labels, where "/" is a display convention
#
# THE DEFECT THESE EXIST TO PREVENT. Gmail's Label resource carries no parent
# field, so `Clients/Smith` is an independent label that whole-name matching
# treats as unrelated to `Clients`. A firm excludes `Clients` because its
# contents are privileged; every matter label underneath it was read anyway,
# with no error and no log line.
#
# The rule is THE PARENT'S ANSWER GOVERNS THE SUBTREE, in both directions, and
# it applies only where the install says names encode nesting. Graph folders
# nest through parent_id and their names do not encode it, so a Graph folder
# named `Clients/Smith` stays one folder.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "folder", "expected"),
    [
        # -- exclusion cascades down (names_encode_nesting=True) --------------
        pytest.param(NESTED_EXCLUDE, "Clients", False, id="nested-excludes-parent"),
        pytest.param(NESTED_EXCLUDE, "Clients/Smith", False, id="nested-excludes-child"),
        pytest.param(NESTED_EXCLUDE, "Clients/Smith/2026", False, id="nested-excludes-grandchild"),
        # -- inclusion cascades down the same way ----------------------------
        pytest.param(NESTED_INCLUDE, "Clients", True, id="nested-includes-parent"),
        pytest.param(NESTED_INCLUDE, "Clients/Smith", True, id="nested-includes-child"),
        pytest.param(NESTED_INCLUDE, "Clients/Smith/2026", True, id="nested-includes-grandchild"),
        # ...but never sideways.
        pytest.param(NESTED_INCLUDE, "Vendors", False, id="nested-refuses-sibling"),
        pytest.param(NESTED_INCLUDE, "Vendors/Acme", False, id="nested-refuses-sibling-child"),
        # -- BOUNDARY match, never a bare startswith --------------------------
        # `MyClients` is not under `Clients`. A substring or bare-prefix test
        # says it is, which hides mail in the exclusion direction and reads
        # unnamed mail in the inclusion direction.
        pytest.param(NESTED_EXCLUDE, "MyClients", True, id="boundary-prefix-not-a-child"),
        pytest.param(NESTED_EXCLUDE, "Clients-Archive", True, id="boundary-hyphen-not-a-child"),
        pytest.param(NESTED_EXCLUDE, "ClientsArchive", True, id="boundary-suffix-not-a-child"),
        pytest.param(NESTED_INCLUDE, "MyClients", False, id="boundary-inclusion-prefix"),
        pytest.param(NESTED_INCLUDE, "Clients-Archive", False, id="boundary-inclusion-hyphen"),
        # -- case folding across the separator --------------------------------
        pytest.param(NESTED_EXCLUDE, "CLIENTS/Smith", False, id="nested-case-parent-upper"),
        pytest.param(NESTED_EXCLUDE, "clients/SMITH", False, id="nested-case-child-upper"),
        pytest.param(
            MailboxScope(
                mode="all_except", excluded_folders=("CLIENTS",), names_encode_nesting=True
            ),
            "clients/smith",
            False,
            id="nested-case-configured-upper",
        ),
        # -- flag OFF: Graph names are flat -----------------------------------
        # A Graph folder named `Clients/Smith` is ONE folder whose name holds a
        # slash; reading nesting out of it invents a parent Graph never stated.
        pytest.param(FLAT_EXCLUDE, "Clients", False, id="flat-excludes-exact"),
        pytest.param(FLAT_EXCLUDE, "Clients/Smith", True, id="flat-child-unaffected"),
        pytest.param(FLAT_INCLUDE, "Clients", True, id="flat-includes-exact"),
        pytest.param(FLAT_INCLUDE, "Clients/Smith", False, id="flat-inclusion-admits-only-named"),
        # Naming the child itself has always worked and must keep working.
        pytest.param(
            MailboxScope(mode="all_except", excluded_folders=("Clients/Smith",)),
            "Clients/Smith",
            False,
            id="flat-explicit-child-name",
        ),
        pytest.param(
            MailboxScope(mode="all_except", excluded_folders=("Clients/Smith",)),
            "Clients",
            True,
            id="flat-explicit-child-leaves-parent",
        ),
    ],
)
def test_nesting_governs_the_subtree_only_when_names_encode_it(
    scope: MailboxScope, folder: str, expected: bool
) -> None:
    assert scope.covers(folder) is expected


# ---------------------------------------------------------------------------
# covers_all() -- Gmail labels, where a message carries many containers at once
#
# The rule is ANY-EXCLUSION-WINS. If ANY container is out of scope the message
# is out of scope: no number of in-scope labels outvotes one out-of-scope one.
# The rejected alternative, any-inclusion-wins, reads exactly the mail a client
# asked us to stay out of -- because privileged mail arrives as a matter email
# that ALSO carries a benign label.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "names", "well_known", "expected"),
    [
        # -- THE defect case, and its order-independence ----------------------
        pytest.param(
            EXCEPT_MATTERS, ["Billing", "Client Matters"], (), False, id="excluded-beats-included"
        ),
        pytest.param(
            EXCEPT_MATTERS, ["Client Matters", "Billing"], (), False, id="exclusion-first-position"
        ),
        pytest.param(
            EXCEPT_MATTERS,
            ["Billing", "Newsletters", "Vendors", "Receipts", "Client Matters"],
            (),
            False,
            id="one-exclusion-outvotes-many",
        ),
        pytest.param(EXCEPT_MATTERS, ["Billing", "Newsletters"], (), True, id="all-included"),
        pytest.param(
            MailboxScope(mode="all_except", excluded_folders=("Client Matters", "Shared Mailbox")),
            ["Client Matters", "Shared Mailbox"],
            (),
            False,
            id="all-excluded",
        ),
        # -- empty sequence fails CLOSED --------------------------------------
        # A label list that failed to populate is a provider bug; "we know
        # nothing" must not resolve to permission.
        pytest.param(ALL_FOLDERS, [], (), False, id="empty-sequence-widest-mode"),
        pytest.param(INBOX_ONLY, [], (), False, id="empty-sequence-narrowest-mode"),
        # -- case and whitespace hold across every element --------------------
        pytest.param(
            EXCEPT_MATTERS, ["billing", "CLIENT MATTERS"], (), False, id="multi-case-exclusion"
        ),
        pytest.param(EXCEPT_MATTERS, ["BILLING", "newsletters"], (), True, id="multi-case-covered"),
        pytest.param(
            EXCEPT_MATTERS,
            ["Billing", "  Client Matters  "],
            (),
            False,
            id="multi-whitespace-exclusion",
        ),
        # -- the modes, in the multi path -------------------------------------
        pytest.param(INBOX_ONLY, ["Inbox"], (), True, id="multi-inbox_only-inbox"),
        pytest.param(
            INBOX_ONLY, ["Inbox", "Client Matters"], (), False, id="multi-inbox_only-extra-label"
        ),
        pytest.param(
            ALL_FOLDERS, ["Billing", "Client Matters", "Vendors"], (), True, id="multi-all_folders"
        ),
        pytest.param(ALL_FOLDERS, ["Billing", "Spam"], (), False, id="multi-system-spam"),
        pytest.param(ALL_FOLDERS, ["Billing", "Trash"], (), False, id="multi-system-trash"),
        pytest.param(ONLY_BILLING, ["Billing"], (), True, id="multi-only_listed-listed"),
        pytest.param(
            ONLY_BILLING, ["Billing", "Client Matters"], (), False, id="multi-only_listed-unlisted"
        ),
        pytest.param(
            MailboxScope(mode="only_listed", included_folders=("Billing", "Receipts")),
            ["Billing", "Receipts"],
            (),
            True,
            id="multi-only_listed-all-listed",
        ),
        # -- well_known pairs positionally, and a short sequence PADS ---------
        pytest.param(
            ALL_FOLDERS, ["Billing", "Geloeschte Elemente"], (), True, id="multi-untagged-localised"
        ),
        pytest.param(
            ALL_FOLDERS,
            ["Billing", "Geloeschte Elemente"],
            [None, "deleted"],
            False,
            id="multi-tagged-localised",
        ),
        pytest.param(
            EXCEPT_MATTERS,
            ["Posteingang", "Billing"],
            ["inbox"],
            True,
            id="short-tag-sequence-pads",
        ),
        pytest.param(
            EXCEPT_MATTERS,
            ["Posteingang", "Client Matters"],
            ["inbox"],
            False,
            id="short-tag-sequence-does-not-misalign",
        ),
        # -- nesting reaches the multi path for free --------------------------
        pytest.param(
            NESTED_EXCLUDE, ["Billing", "Clients/Smith"], (), False, id="multi-nested-child"
        ),
        pytest.param(
            NESTED_EXCLUDE, ["Billing", "MyClients"], (), True, id="multi-nested-boundary"
        ),
    ],
)
def test_covers_all_refuses_a_message_when_any_container_is_out_of_scope(
    scope: MailboxScope,
    names: list[str],
    well_known: list[str | None],
    expected: bool,
) -> None:
    assert scope.covers_all(names, well_known=well_known) is expected


def test_a_single_element_sequence_matches_the_single_name_answer() -> None:
    """The multi path must not disagree with ``covers`` on a one-element list."""
    assert EXCEPT_MATTERS.covers_all(["Billing"]) == EXCEPT_MATTERS.covers("Billing")
    assert EXCEPT_MATTERS.covers_all(["Client Matters"]) == EXCEPT_MATTERS.covers("Client Matters")


# ---------------------------------------------------------------------------
# Validation -- malformed config is a startup error, never a quiet default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        # A list-driven mode with an empty list means the human was asked and
        # answered nothing. Guessing either way is wrong.
        pytest.param({"mode": "all_except"}, "excluded_folders is empty", id="all_except-no-list"),
        pytest.param(
            {"mode": "only_listed"}, "included_folders is empty", id="only_listed-no-list"
        ),
        pytest.param(
            {"mode": "all_except", "excluded_folders": ("Client Matters", "   ")},
            "must not contain an empty name",
            id="blank-name-in-exclusions",
        ),
        pytest.param(
            {"mode": "only_listed", "included_folders": ("",)},
            "must not contain an empty name",
            id="blank-name-in-inclusions",
        ),
        pytest.param(
            {
                "mode": "all_except",
                "excluded_folders": tuple(f"Folder {n}" for n in range(MAX_SCOPE_FOLDERS + 1)),
            },
            "the limit is",
            id="list-one-over-the-cap",
        ),
        # A folder in both lists has no defensible answer, and a tie-break
        # would make a privileged folder's fate ours rather than the client's.
        pytest.param(
            {
                "mode": "only_listed",
                "included_folders": ("Billing",),
                "excluded_folders": ("Billing",),
            },
            "both lists",
            id="same-folder-both-lists",
        ),
        pytest.param(
            {
                "mode": "only_listed",
                "included_folders": ("Billing",),
                "excluded_folders": ("BILLING",),
            },
            "both lists",
            id="both-lists-differing-case",
        ),
        pytest.param({"mode": "everything"}, None, id="unknown-mode"),
        # Section forbids extras, so a typo is a startup error not a default.
        pytest.param({"include_drafts": True}, None, id="unknown-field"),
    ],
)
def test_malformed_scope_input_is_refused_at_construction(
    kwargs: dict[str, object], match: str | None
) -> None:
    with pytest.raises(ValidationError, match=match):
        MailboxScope(**kwargs)  # type: ignore[arg-type]


def test_accepted_folder_lists_are_normalised_and_bounded() -> None:
    """The accepting side of the validator, which the refusals cannot show.

    A list exactly AT the cap must be accepted -- an off-by-one here refuses a
    legitimate config, and the refusal table above would not notice.
    """
    stripped = MailboxScope(mode="all_except", excluded_folders=("  Client Matters  ",))
    assert stripped.excluded_folders == ("Client Matters",)
    assert stripped.covers("Client Matters") is False

    at_cap = tuple(f"Folder {index}" for index in range(MAX_SCOPE_FOLDERS))
    assert len(MailboxScope(mode="all_except", excluded_folders=at_cap).excluded_folders) == (
        MAX_SCOPE_FOLDERS
    )


def test_the_model_is_frozen() -> None:
    """Scope is answered once at install; nothing may edit it at runtime."""
    scope = MailboxScope()
    with pytest.raises(ValidationError):
        scope.mode = "all_folders"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The per-provider nesting default -- the hole the field alone did not close
#
# ``names_encode_nesting`` defaults to False on the model so an upgrade cannot
# change what any install reads. Left at that default on Gmail, though, the
# original defect survives untouched: an exclusion naming a privileged parent
# label reads every matter beneath it, because Gmail's Label resource has no
# parent and the slash is display convention. These tests are what makes the
# Gmail install arrive with the flag ON, and -- just as importantly -- what
# stops that default from overruling a client who answered differently.
# ---------------------------------------------------------------------------


def test_the_default_scope_table_answers_nesting_per_provider() -> None:
    """Gmail nests by name, Graph does not, and both rows stay narrow otherwise.

    The table must not fall behind ``PROVIDER_NAMES``: a provider added with no
    row would fill from nothing and silently drop out of the per-provider
    default. And the nesting flag changes what an exclusion MEANS, never what
    is read -- a row that widened ``mode`` would be a data-exposure change
    delivered by a default nobody edited.
    """
    assert set(DEFAULT_SCOPES) == set(PROVIDER_NAMES)
    assert DEFAULT_SCOPES[GOOGLE].names_encode_nesting is True
    assert DEFAULT_SCOPES[MICROSOFT].names_encode_nesting is False
    for provider in PROVIDER_NAMES:
        scope = DEFAULT_SCOPES[provider]
        assert scope.mode == "inbox_only"
        assert scope.excluded_folders == ()
        assert scope.included_folders == ()
        assert (scope.include_junk, scope.include_deleted, scope.include_archive) == (
            False,
            False,
            False,
        )


def test_the_gmail_default_reaches_covers_rather_than_merely_being_set() -> None:
    """Asserting the flag alone passes even if ``covers`` stopped consulting it."""
    scope = DEFAULT_SCOPES[GOOGLE].model_copy(
        update={"mode": "all_except", "excluded_folders": ("Clients",)}
    )
    assert scope.covers("Clients") is False
    assert scope.covers(f"Clients{NESTING_SEPARATOR}Smith") is False
    assert scope.covers("MyClients") is True


def test_an_explicit_nesting_answer_beats_the_provider_default_both_ways() -> None:
    """The default is a starting value, not a policy the installer imposes.

    If it could not be turned off it would stop being a default and become a
    provider-name inference by another route -- and the override has to work in
    BOTH directions, or a Graph tenant whose folders are slash-named by
    convention cannot say so.
    """
    off = ConnectorConfig(scope={GOOGLE: MailboxScope(names_encode_nesting=False)})
    assert off.mailbox_scope(GOOGLE).names_encode_nesting is False
    on = ConnectorConfig(scope={MICROSOFT: MailboxScope(names_encode_nesting=True)})
    assert on.mailbox_scope(MICROSOFT).names_encode_nesting is True


def test_the_nesting_answer_survives_the_installer_payload(tmp_path: Path) -> None:
    """A stated answer must reach the disk and come back unchanged.

    ``load_config`` fills absent providers from the table, so a persisted False
    is the one value the fill could plausibly stomp on the way back in. The
    written defaults are checked in the same pass because the payload is what a
    human opens to check the posture.
    """
    stated = tmp_path / "stated.json"
    write_config(ConnectorConfig(scope={GOOGLE: MailboxScope(names_encode_nesting=False)}), stated)
    payload = json.loads(stated.read_text(encoding="utf-8"))["mailbox_scope"]
    assert payload[GOOGLE]["names_encode_nesting"] is False
    reloaded = load_config(stated)
    assert reloaded.mailbox_scope(GOOGLE).names_encode_nesting is False
    assert reloaded.mailbox_scope(MICROSOFT).names_encode_nesting is False

    defaults = tmp_path / "defaults.json"
    write_config(default_config(), defaults)
    written = json.loads(defaults.read_text(encoding="utf-8"))["mailbox_scope"]
    assert written[GOOGLE]["names_encode_nesting"] is True
    assert written[MICROSOFT]["names_encode_nesting"] is False
    loaded = load_config(defaults)
    assert loaded.mailbox_scope(GOOGLE).names_encode_nesting is True
    assert loaded.mailbox_scope(MICROSOFT).names_encode_nesting is False


def test_a_nesting_scope_round_trips_and_still_refuses_the_child(tmp_path: Path) -> None:
    """The flag is a data-exposure setting, so it must round-trip as BEHAVIOUR."""
    target = tmp_path / "connector-config.json"
    write_config(ConnectorConfig(scope={GOOGLE: NESTED_EXCLUDE}), target)
    loaded = load_config(target).mailbox_scope(GOOGLE)
    assert loaded.names_encode_nesting is True
    assert loaded.covers("Clients/Smith") is False
    assert loaded.covers("MyClients") is True
