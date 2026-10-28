#!/usr/bin/env python3
"""Judge a built kit zip: prove the artifact physically lacks what it must lack.

WHY THIS SCRIPT EXISTS
    Issue #107 ruled that tiers are NOT config gating. The basic kit is sold as
    an artifact WITHOUT drafting intelligence, and "without" has to mean the
    bytes are absent -- not that a boolean in ``config.json`` is ``false``. A
    flag is a promise the vendor makes about a program the buyer cannot read;
    a zip that does not contain ``create_draft`` anywhere is a fact the buyer
    checks with ``unzip -p`` and a text search. Only the second one survives
    the customer who upgrades the connector, edits their own config, or hands
    the folder to another agent.

    So the packaging step ends in an ASSERTION over the finished artifact, the
    same shape ``tests/test_containment.py`` uses against the source tree: a
    scanner that looks at what is actually there and refuses it by name. The
    thing shipped is the thing judged -- not the build script's intent, and not
    a manifest describing what the build script meant to do.

WHY THE RULES ARE DATA
    Every rule lives in ``tiers.json`` beside this file, per the
    determinism-first and config-as-data laws. Adding a tier, or a newly
    discovered leak pattern, is an edit to a JSON list -- never a new branch in
    Python. This file owns the MECHANISM (walk members, match globs, read text)
    and nothing about WHICH paths matter.

WHAT IT CHECKS, IN ORDER
    1. ``never_ship`` -- refused in every tier. Encodes the Rico 2026-08-21
       ruling that internal process files (``_DOCS/``, ``HANDOFF-RULES.md``,
       ``GOTCHAS*``, ``_graph/``, ``_reports/``) never enter a shipped zip, plus
       build residue (``__pycache__``, tool caches) and the real
       ``secrets/config.json``, which never ships at all.
    2. The tier's ``exclude`` globs -- paths this tier may not contain.
    3. The tier's ``forbidden_content`` -- substrings refused inside shipped
       text members. Path exclusion alone is not enough: a helper MOVED out of
       ``services/voice/`` still carries the capability, so the excluded
       members' own MODULE PATHS stay refused by content. A text member whose
       bytes are not UTF-8 is itself a violation here rather than a skip.
    4. The tier's ``config_pins`` -- exact values the shipped
       ``config/connector-config.json`` must carry. This is what a tier's
       CAPABILITY assertion looks like once #146 proved a substring scan cannot
       be one: the basic kit is sold as file-only, so the artifact must pin
       ``posture: file_only`` with drafting off for every provider, and that is
       a fact in the shipped bytes rather than a promise about them.
    5. The CUT -- which of D4's two artifacts this is. Without ``--with-tests``
       the archive must carry no ``tests/`` member and no ``TESTS-NOTE.md``;
       with it, it must carry both. The filename is not the evidence: a zip
       named ``-with-tests`` whose suite never made it in is the wrong artifact
       in a customer's hands, and the name is what made it look right.
    6. The self-contained floor -- ``FLOOR_MEMBERS``. An artifact missing any
       of them cannot be installed by the MSP who receives it, which is a
       packaging defect in exactly the same way a leak is.

CUSTOM-BUILD MODE (``--custom``)
    ``build_custom.py`` produces a MANAGED-INSTALL artifact: the same tier cut,
    plus a real ``secrets/config.json`` written from the vault, because the
    whole promise of a managed install is that the client never touches
    configuration. ``never_ship`` refuses that member in every tier -- rightly,
    since it must never enter a KIT zip -- so the custom artifact needs its own
    mode instead of a hole punched in the manifest. The exemption then applies
    to the one artifact class that earns it, and the shipped-kit rules stay
    exactly as strict as they were.

    ``--custom`` is a FLAG and not a tier name, because the two are orthogonal.
    A custom build still cuts ``enhanced`` or ``basic`` (the profile's
    ``zip_tier``, which is why the tier stays a positional argument here), and
    every tier rule still applies to it. A ``"custom"`` entry in ``tiers.json``
    could only duplicate whichever real tier it wrapped, and the duplicate is
    the copy that drifts.

    In this mode the judge additionally REQUIRES ``secrets/config.json`` and
    reads it: it must parse as JSON, and no leaf string may be empty or carry
    ``REPLACE-ME`` or ``TBD``. An EMPTY payload is refused too unless the
    archive carries ``SECRETLESS_COMMENT`` -- the marker ``build_custom.py``
    stamps when the profile declares no ``secrets`` at all -- because ``{}``
    otherwise cannot be told apart from a build whose every vault read failed.
    It also scans ``config/connector-config.json`` for the same placeholder
    markers, since a managed install whose CONFIG still says ``REPLACE-ME``
    cannot authenticate either. An unfilled credential is a build that silently
    did not resolve, and the client discovers it as an install that cannot
    authenticate. Violations name the KEY PATH ONLY and never the value --
    this judge's stdout lands in terminals, CI logs, and handoff notes, so a
    message quoting the offending leaf would put a live credential in all
    three.

    Every violation is printed, never just the first. A judge that stops early
    costs the packager one build round trip per mistake.

Exit codes:
    0  the artifact is clean for this tier
    1  at least one violation -- each printed as its own line
    3  harness error; the check could not run and told you nothing

Usage:
    uv run python scripts/packaging/assert_zip.py <zip> <tier>
    uv run python scripts/packaging/assert_zip.py --with-tests <zip> <tier>
    uv run python scripts/packaging/assert_zip.py --custom <zip> <tier>
"""

from __future__ import annotations

import fnmatch
import json
import re
import sys
import zipfile
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "tiers.json"
"""The data manifest, read from beside this file rather than from the cwd.

The judge is invoked from the connector root during a packaging run, but a
cwd-relative rules path would silently pick up a different manifest when it is
invoked from anywhere else -- and a packaging check that reads the wrong rules
prints a green receipt for an artifact nobody judged.
"""

FLOOR_MEMBERS = (
    "install.sh",
    "install.ps1",
    "AGENTS.md",
    "INSTALL-RUNBOOK.md",
    "pyproject.toml",
    "config/connector-config.json",
    "secrets/config.example.json",
)
"""What an installable artifact must contain regardless of tier.

Grown in #145. The original four proved an artifact had SOURCE and INSTRUCTIONS
and stopped there, which let three install-blocking absences through:
``install.ps1`` is the only installer a Windows client can run, and the kit is
sold to Outlook/M365 shops; ``config/connector-config.json`` is the file the
runbook tells the installer to edit, so an artifact without it has nothing to
configure; ``secrets/config.example.json`` is the shape the installer copies to
``secrets/config.json``, and without it the credential file has to be guessed.
Each absence surfaces at the client's machine rather than at package time,
which is precisely the trade the floor exists to prevent.
"""

TEXT_SUFFIXES = (".py", ".md", ".sh", ".ps1", ".json", ".toml")
"""Members whose bytes are scanned for ``forbidden_content`` and placeholders.

Was ``(".py", ".md")``, and the narrowness was a hole rather than a discipline
(#145): the installers are ``.sh``/``.ps1``, the shipped configuration is
``.json``, and the packaging metadata is ``.toml`` -- all of them text a human
reads, all of them places a capability reference or an unfilled placeholder
actually lives. A client 0.2.8 artifact shipped a literal ``REPLACE-ME``
client id inside ``config/connector-config.json`` and the scan never opened the
file.

Still deliberately a suffix allowlist rather than "everything": a false hit
inside a compiled blob is noise a packager cannot act on. What changed is which
text counts as text.
"""


SECRETS_MEMBER = "secrets/config.json"
"""The member a custom build MUST carry and a kit build must never carry."""

CONFIG_MEMBER = "config/connector-config.json"
"""The shipped connector configuration, scanned for placeholders in custom mode."""

SECRETLESS_COMMENT = b"inbox-zero-connector: profile declared no secrets"
"""The zip-comment marker a custom build writes when its profile has no ``secrets``.

WHY A MARKER EXISTS AT ALL
    An empty ``secrets/config.json`` -- literally ``{}`` -- has two possible
    causes and the judge cannot tell them apart from the bytes: either the
    profile genuinely declares no secrets (the connector's Microsoft path needs
    only a public client id, which lives in the connector config, so a
    secretless managed install is a real and correct artifact), or the profile
    DID declare secrets and every one of them silently failed to resolve.
    ``_unfilled_leaves({})`` returns ``[]`` for both, so the second case shipped
    green: a client 0.2.8 zip judged ``OK`` with ``{}`` inside it (#145).

WHY THE ZIP COMMENT AND NOT A MEMBER
    The declaration has to travel WITH the artifact, because the judge is run
    against a zip handed over by someone who does not have the profile. A new
    member would have to be added to ``FLOOR_MEMBERS`` and ``ship`` and would
    then be a file a client can edit; the archive comment is metadata the
    format already carries, is one line, and changes no member list. It is not
    a security control -- anyone can rewrite it -- and it does not need to be:
    it defends against a SILENT build failure, not against a forger, and a
    forger already has the zip.

    The declaration itself comes from the profile: ``"secrets"`` ABSENT or
    ``{}`` means secretless and the builder writes this comment; ``"secrets"``
    present and non-empty means the resolved payload must be non-empty and
    fully filled. Explicit-vs-absent is the smallest honest mechanism because
    the profile is where the operator already states intent.
"""

UNFILLED_MARKERS = ("REPLACE-ME", "TBD")
"""Substrings that mark a credential the build did not actually resolve.

``REPLACE-ME`` is what ``build_zips.py`` leaves in a shippable kit; ``TBD`` is
the ``TBD-#NN`` convention ``profiles/README.md`` documents for a value blocked
on a GitHub issue. Either one reaching a CUSTOM zip means the managed install
ships without working credentials.
"""


def _load_rules() -> dict:
    """Read and validate ``tiers.json``, or raise for the exit-3 path."""
    with RULES_PATH.open(encoding="utf-8") as handle:
        rules = json.load(handle)
    for key in ("never_ship", "tiers"):
        if key not in rules:
            raise KeyError(f"{RULES_PATH} is missing required key {key!r}")
    return rules


def _matches(member: str, pattern: str) -> bool:
    """Match ``member`` against a glob where a leading ``**/`` may span zero dirs.

    ``fnmatch`` and ``PurePath.match`` both read ``**/HANDOFF-RULES.md`` as
    requiring at least one directory component, so a root-level
    ``HANDOFF-RULES.md`` would slip past the rule written to catch it. That is
    the opposite of what the manifest reads as saying, and a leak the check was
    written to stop. Translating the pattern ourselves keeps the manifest
    readable -- ``**/`` means "anywhere, including here".
    """
    if fnmatch.fnmatch(member, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(member, pattern[3:])
    return False


def _member_names(zip_path: Path) -> list[str]:
    """Every non-directory member of the archive, as posix-style arcnames."""
    with zipfile.ZipFile(zip_path) as archive:
        return [info.filename for info in archive.infolist() if not info.is_dir()]


class UndecodableMemberError(Exception):
    """A member with a text suffix whose bytes are not UTF-8.

    Raised rather than returned so the caller must decide, and every caller
    decides the same way: a VIOLATION. See ``_text_of``.
    """


def _text_of(zip_path: Path, member: str) -> str | None:
    """Decode a scannable member, ``None`` when its suffix is not scanned.

    Raises:
        UndecodableMemberError: The suffix says text and the bytes are not UTF-8.

    THIS USED TO FAIL OPEN AND #145 CLOSED IT. The previous behaviour printed
    ``SKIP undecodable text member`` and returned ``None``, which made the
    member indistinguishable from a binary the scan was never meant to open.
    That is the wrong default for a containment check: an attacker or an
    accident that puts non-UTF-8 bytes into a ``.py`` gets the content scan
    skipped for that member and a green receipt for the artifact. Every real
    ``.py``/``.md``/``.sh``/``.ps1``/``.json``/``.toml`` in this tree is UTF-8,
    so the honest reading of undecodable-text-suffix is "this artifact is not
    what the manifest says it is", and that is a finding.
    """
    if not member.endswith(TEXT_SUFFIXES):
        return None
    with zipfile.ZipFile(zip_path) as archive:
        raw = archive.read(member)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UndecodableMemberError(member) from error


def _check_globs(members: list[str], patterns: list[str], label: str) -> list[str]:
    """Violations for members matching any pattern, one line per (member, pattern)."""
    return [
        f"VIOLATION {label} {member} [{pattern}]"
        for member in members
        for pattern in patterns
        if _matches(member, pattern)
    ]


def _check_content(zip_path: Path, members: list[str], patterns: list[str]) -> list[str]:
    """Violations for shipped text members containing a forbidden substring.

    Walks every text member even when ``patterns`` is empty, because the walk
    is also what surfaces an undecodable member -- a finding in its own right
    (see ``_text_of``) that must not depend on the tier happening to forbid
    some string.
    """
    violations: list[str] = []
    for member in members:
        try:
            text = _text_of(zip_path, member)
        except UndecodableMemberError as exc:
            # Not a fail-open: the unreadable member becomes a reported
            # violation, which is this judge's contract. Logged to stderr with
            # the type only -- member bytes never reach a log.
            print(f"assert_zip: {member} undecodable ({exc})", file=sys.stderr)
            violations.append(f"VIOLATION undecodable_text_member {member}")
            continue
        if text is None:
            continue
        violations.extend(
            f"VIOLATION forbidden_content {member} [{pattern}]"
            for pattern in patterns
            if re.search(re.escape(pattern), text)
        )
    return violations


def _leaf_at(payload: object, dotted: str) -> tuple[bool, object]:
    """Follow a dotted key path into nested dicts; ``(found, value)``."""
    node: object = payload
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _check_config_pins(zip_path: Path, members: list[str], pins: dict) -> list[str]:
    """Violations for a tier whose shipped connector config does not match its pins.

    THE CAPABILITY ASSERTION #146 ASKED FOR. Substring matching on
    ``create_draft`` could never express "this tier is sold without drafting":
    the string appears in the audit ledger, the security-surface tests, and the
    provider protocol, all of which the basic tier legitimately ships, so the
    old ``forbidden_content`` list produced 63 violations on a build that was
    otherwise correct and the SKU could only ship by skipping the judge.

    What actually distinguishes the tier is a POSTURE the artifact carries: the
    basic kit ships ``posture: file_only`` with drafting off for every provider,
    so the connector it installs cannot reach a compose endpoint no matter what
    the buyer's mailbox permits. That is checkable in the shipped bytes -- the
    config member IS shipped -- and unlike a string scan it says something the
    buyer can act on.

    Pins are DATA in ``tiers.json`` (``tiers.<name>.config_pins``), dotted key
    paths to exact expected values, so a new tier is a manifest edit.

    A pin naming an ABSENT key is a violation, not a pass: a config missing
    ``posture`` entirely falls back to whatever the code defaults to, which is
    the thing the pin exists to stop being implicit.
    """
    if not pins:
        return []
    if CONFIG_MEMBER not in members:
        return [f"VIOLATION config_pin_no_config {CONFIG_MEMBER}"]
    with zipfile.ZipFile(zip_path) as archive:
        raw = archive.read(CONFIG_MEMBER)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Not a fail-open: the unparseable config becomes a reported
        # violation. Type only -- payload may hold identifiers.
        print(f"assert_zip: {CONFIG_MEMBER} did not parse ({type(exc).__name__})", file=sys.stderr)
        return [f"VIOLATION config_pin_unparseable {CONFIG_MEMBER}"]

    violations: list[str] = []
    for dotted, expected in pins.items():
        found, actual = _leaf_at(payload, dotted)
        if not found:
            violations.append(f"VIOLATION config_pin_missing {CONFIG_MEMBER} [{dotted}]")
        elif actual != expected:
            violations.append(
                f"VIOLATION config_pin {CONFIG_MEMBER} [{dotted} is {actual!r}, "
                f"tier requires {expected!r}]"
            )
    return violations


def _check_config_placeholders(zip_path: Path, members: list[str]) -> list[str]:
    """Violations for an UNFILLED placeholder in a custom build's connector config.

    ``UNFILLED_MARKERS`` used to be scanned only inside ``secrets/config.json``,
    and #145 is what that missed: a client 0.2.8 managed-install zip
    shipped ``credentials.google_client_id`` reading
    ``REPLACE-ME.apps.googleusercontent.com`` in ``config/connector-config.json``
    and judged clean. A managed install is defined by the client never touching
    configuration, so a placeholder in the CONFIG is exactly as fatal as one in
    the secrets -- the connector cannot authenticate either way.

    Kit builds are untouched: ``REPLACE-ME`` in a shippable kit is the correct
    content, and the runbook tells the buyer to replace it. This check runs in
    ``--custom`` mode only.

    Names key paths, never values, for the reason ``_check_custom_secrets``
    does.
    """
    if CONFIG_MEMBER not in members:
        return []
    with zipfile.ZipFile(zip_path) as archive:
        raw = archive.read(CONFIG_MEMBER)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Not a fail-open: converted into a reported violation. Type only.
        print(f"assert_zip: {CONFIG_MEMBER} did not parse ({type(exc).__name__})", file=sys.stderr)
        return [f"VIOLATION custom_config_unparseable {CONFIG_MEMBER}"]
    return [
        f"VIOLATION custom_config_unfilled {CONFIG_MEMBER} [{key_path}]"
        for key_path in _unfilled_leaves(payload, markers_only=True)
    ]


def _unfilled_leaves(node: object, path: str = "", *, markers_only: bool = False) -> list[str]:
    """Key paths whose leaf string is empty or still carries a placeholder.

    Returns PATHS, never values. Non-string leaves are ignored: the check is
    about credentials that were never filled in, and a boolean or a number is
    not a credential shape.

    Args:
        node: The parsed payload, or a subtree of it during recursion.
        path: Dotted key path accumulated so far.
        markers_only: When true, an EMPTY string is not a finding and only an
            ``UNFILLED_MARKERS`` hit is. Set for the connector config, where an
            empty string is real configuration -- ``folder_prefix: ""`` means
            "no prefix", which is the shipped default -- rather than a
            credential nobody resolved. Left false for ``secrets/config.json``,
            where every leaf is a credential and an empty one is a vault field
            that came back with nothing.
    """
    if isinstance(node, dict):
        return [
            found
            for key, value in node.items()
            for found in _unfilled_leaves(
                value, f"{path}.{key}" if path else str(key), markers_only=markers_only
            )
        ]
    if isinstance(node, list):
        return [
            found
            for index, value in enumerate(node)
            for found in _unfilled_leaves(value, f"{path}[{index}]", markers_only=markers_only)
        ]
    if isinstance(node, str):
        has_marker = any(marker in node for marker in UNFILLED_MARKERS)
        if has_marker or (not markers_only and not node.strip()):
            return [path or "<root>"]
    return []


def _declared_secretless(zip_path: Path) -> bool:
    """Whether the builder stamped this archive as built from a secretless profile."""
    with zipfile.ZipFile(zip_path) as archive:
        return archive.comment.strip() == SECRETLESS_COMMENT


def _check_custom_secrets(zip_path: Path, members: list[str]) -> list[str]:
    """Violations for the custom artifact's ``secrets/config.json``.

    Requires the member, requires it to parse, and requires every leaf string
    to be filled. Every line names the member and the key path and nothing
    else, so this function can never be the thing that leaks a credential into
    a log.

    AN EMPTY PAYLOAD IS THE CASE #145 CAUGHT. ``_unfilled_leaves({})`` is ``[]``
    -- correctly, there are no unfilled leaves -- so a build whose vault
    resolution produced nothing at all passed vacuously, and a client
    0.2.8 managed-install zip judged ``OK`` carrying a literal ``{}``. Emptiness
    is now only acceptable when the ARTIFACT DECLARES it, via the zip comment
    ``build_custom.py`` writes when the profile has no ``secrets`` key (see
    ``SECRETLESS_COMMENT``). A secretless managed install is a real artifact --
    the Microsoft public-client path needs no secret at all -- so the answer is
    to make the operator state it rather than to forbid it.
    """
    if SECRETS_MEMBER not in members:
        return [f"VIOLATION custom_missing_secrets {SECRETS_MEMBER}"]
    with zipfile.ZipFile(zip_path) as archive:
        raw = archive.read(SECRETS_MEMBER)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Not a fail-open: the unparseable payload is converted into a reported
        # violation, which is this function's contract. Logged to stderr with
        # the exception type only -- never the payload, which holds secrets.
        print(
            f"assert_zip: {SECRETS_MEMBER} did not parse ({type(exc).__name__})",
            file=sys.stderr,
        )
        return [f"VIOLATION custom_secrets_unparseable {SECRETS_MEMBER}"]
    violations = [
        f"VIOLATION custom_secrets_unfilled {SECRETS_MEMBER} [{key_path}]"
        for key_path in _unfilled_leaves(payload)
    ]
    if not payload and not _declared_secretless(zip_path):
        violations.append(
            f"VIOLATION custom_secrets_empty {SECRETS_MEMBER} "
            "[no resolved secrets and the profile did not declare itself secretless]"
        )
    return violations


TESTS_PREFIX = "tests/"
"""Member prefix that decides which cut an artifact is."""

TESTS_NOTE_MEMBER = "TESTS-NOTE.md"
"""The note the with-tests cut must carry at its root; written by ``build_zips``."""


def _check_cut(members: list[str], *, with_tests: bool) -> list[str]:
    """Violations for an artifact that is not the cut it claims to be.

    D4 (2026-09-01) makes each tier two artifacts, and the difference between
    them is exactly two facts about the bytes: whether ``tests/`` is present,
    and whether the fixture note is. Both are checked here rather than trusted
    from the filename, for the reason the whole judge exists -- a cut named
    ``-with-tests`` that carries no suite, or a plain cut that carries one, is
    the wrong artifact in a customer's hands and the name is what made it look
    right.
    """
    has_tests = any(member.startswith(TESTS_PREFIX) for member in members)
    has_note = TESTS_NOTE_MEMBER in members
    violations: list[str] = []
    if with_tests:
        if not has_tests:
            violations.append(f"VIOLATION missing_tests_in_with_tests_cut {TESTS_PREFIX}")
        if not has_note:
            violations.append(f"VIOLATION missing_tests_note {TESTS_NOTE_MEMBER}")
    else:
        if has_tests:
            violations.append(f"VIOLATION tests_in_plain_cut {TESTS_PREFIX}")
        if has_note:
            violations.append(f"VIOLATION unexpected_tests_note {TESTS_NOTE_MEMBER}")
    return violations


def _check_floor(members: list[str]) -> list[str]:
    """Violations for floor files the artifact does not contain at its root."""
    present = set(members)
    return [
        f"VIOLATION missing_floor {required}"
        for required in FLOOR_MEMBERS
        if required not in present
    ]


def _all_violations(
    zip_path: Path, members: list[str], rules: dict, tier: dict, *, custom: bool
) -> list[str]:
    """Every violation this artifact carries, in check order.

    The CUT check is ``main``'s, not this function's: it depends on the
    invocation flag rather than on the manifest, and folding it in here would
    push this signature past the argument limit for no gain in readability.

    Extracted from ``main`` so the check SEQUENCE is one readable list rather
    than a tail of accumulating statements inside argument parsing and error
    handling. Order matters only for the receipt's readability -- every check
    runs regardless, because a judge that stops early costs the packager one
    build round trip per mistake.
    """
    # In custom mode the ONE exempt member is dropped from the set the glob
    # checks see, rather than the glob checks being skipped. Everything else
    # -- including every other ``never_ship`` entry -- is judged unchanged.
    glob_members = [m for m in members if m != SECRETS_MEMBER] if custom else members

    violations = _check_globs(glob_members, rules["never_ship"], "never_ship")
    violations += _check_globs(glob_members, tier.get("exclude", []), "tier_exclude")
    violations += _check_content(zip_path, glob_members, tier.get("forbidden_content", []))
    violations += _check_config_pins(zip_path, members, tier.get("config_pins", {}))
    violations += _check_floor(members)
    if custom:
        violations += _check_custom_secrets(zip_path, members)
        violations += _check_config_placeholders(zip_path, members)
    return violations


def _parse_argv(argv: list[str]) -> tuple[Path, str, bool, bool] | None:
    """Split argv into ``(zip_path, tier, custom, with_tests)``, ``None`` when unusable.

    Hand-rolled rather than ``argparse`` to keep the existing positional
    contract byte-identical for every caller that already invokes this judge as
    ``assert_zip.py <zip> <tier>``. Both flags may appear in either order
    before the positionals; an unknown flag is unusable rather than ignored,
    because a typo'd flag silently judging the wrong cut is the failure this
    whole file exists to prevent.
    """
    flags = {arg for arg in argv if arg.startswith("-")}
    if flags - {"--custom", "--with-tests"}:
        return None
    rest = [arg for arg in argv if not arg.startswith("-")]
    if len(rest) != 2:
        return None
    return Path(rest[0]), rest[1], "--custom" in flags, "--with-tests" in flags


def main(argv: list[str]) -> int:
    """Judge one zip against one tier and print the receipt to stdout.

    ``--custom`` selects custom-build mode; see the module docstring for what
    that changes and why it is a flag rather than a tier.
    """
    parsed = _parse_argv(argv)
    if parsed is None:
        print("usage: assert_zip.py [--custom] [--with-tests] <zip> <tier>", file=sys.stderr)
        return 3
    zip_path, tier_name, custom, with_tests = parsed

    try:
        rules = _load_rules()
        tier = rules["tiers"][tier_name]
        members = _member_names(zip_path)
    except KeyError as error:
        print(f"harness error: unknown tier or malformed rules: {error}", file=sys.stderr)
        return 3
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        print(f"harness error: {error}", file=sys.stderr)
        return 3

    violations = _all_violations(zip_path, members, rules, tier, custom=custom)
    violations += _check_cut(members, with_tests=with_tests)

    for line in violations:
        print(line)
    if violations:
        return 1

    mode = "custom" if custom else "kit"
    cut = "with-tests" if with_tests else "no-tests"
    print(f"OK {zip_path.name} mode={mode} tier={tier_name} cut={cut} members={len(members)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
