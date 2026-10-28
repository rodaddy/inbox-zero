#!/usr/bin/env python3
"""Build the shippable kit zips from this one working tree, one tier at a time.

WHY THIS SCRIPT EXISTS
    Issue #107 ruled that the tiers are BUILD OUTPUTS of a single codebase,
    not separate repos and not separate branches. There is one connector; the
    basic kit is that connector with named paths withheld at package time. The
    alternative -- a forked tree per tier -- means every bug fix has to be
    landed twice, and the second landing is the one that silently does not
    happen. Keeping the fork at the ZIP boundary means the source of truth
    stays singular and the divergence lives in one JSON list.

WHY THE RULES ARE DATA
    ``tiers.json`` beside this file owns every path decision: what may ship at
    all (``ship``), what is refused in every tier (``never_ship``), and what a
    given tier withholds (``tiers.<name>.exclude``). Per the determinism-first
    and config-as-data laws, adding a tier is an edit to that manifest, never
    a new branch here. This file owns the MECHANISM -- walk, filter, write --
    and knows nothing about which paths matter.

    The same manifest is read by ``assert_zip.py``, which judges the finished
    artifact. Builder and judge sharing one source of rules is deliberate but
    is NOT the proof: the judge re-derives its verdict from the bytes actually
    in the zip, so a builder bug shows up as a failed assertion rather than as
    a green build of a wrong artifact.

    THE BUILDER NOW RUNS THAT JUDGE ITSELF (#145). It used to only PRINT the
    command, on the reasoning that the exit code should keep meaning "the zip
    was written". That reasoning inverted the risk: an unjudged zip is
    indistinguishable from a judged one on disk, so the artifact most likely to
    reach a client is the one built by whoever did not paste the second
    command. The exit code now means "the zip was written AND it passed", which
    is the only claim a packager should be able to make without a second step.
    The command is still printed, because a green build the operator can re-run
    by hand is what makes the receipt checkable by someone who was not there.

WHY EACH TIER PRODUCES TWO CUTS
    D4 (2026-09-01) ruled that ``tests/`` is neither always shipped nor
    always withheld: some customers want the suite they can run, and some
    reviewers read the draft/voice fixture strings under ``tests/`` as product
    behaviour. Deciding it once in ``tiers.json`` would pick wrong for one of
    them, so every tier build writes BOTH -- ``...-<version>.zip`` without the
    suite and ``...-<version>-with-tests.zip`` with it -- and the operator picks which
    one a given customer receives. The with-tests cut carries a generated
    ``TESTS-NOTE.md`` at its root saying the fixtures are fixtures, because the
    person who opens the zip is not the person who was told.

WHY THE OUTPUT IS BYTE-STABLE
    Every member is written with a fixed ``date_time`` of (1980, 1, 1) and the
    member order is sorted. Two builds of the same commit therefore produce
    identical archives, which is what lets "this zip came from that commit" be
    checked instead of trusted. Real mtimes would make every rebuild a new
    artifact for no reason and destroy that property.

WHAT IT NEVER READS
    ``secrets/config.json`` -- the real credential file. It is refused by the
    ``never_ship`` list AND is not reachable from the ``ship`` allowlist, which
    names only ``secrets/config.example.json`` and ``secrets/README.md``. Two
    independent mechanisms, because a credential leak is the one packaging
    defect that cannot be fixed after the artifact is sent.

Exit codes:
    0  every requested tier was written AND passed its own judge
    1  a real finding -- a ship entry is missing from the tree, or the written
       artifact failed ``assert_zip.py``
    3  harness error; the build could not run and told you nothing

Usage:
    uv run python scripts/packaging/build_zips.py                  # all tiers
    uv run python scripts/packaging/build_zips.py --tier enhanced
    uv run python scripts/packaging/build_zips.py --tier basic --out /some/dir
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import zipfile
from pathlib import Path

CONNECTOR_ROOT = Path(__file__).resolve().parents[2]
"""The app root, resolved from THIS file rather than from the cwd.

A cwd-relative root would build a different (and probably empty) artifact when
the script is invoked from anywhere but the connector directory, and an empty
zip is a defect that looks exactly like a successful build.
"""

RULES_PATH = Path(__file__).resolve().parent / "tiers.json"
"""The data manifest, read from beside this file for the same reason."""

MODE_EXECUTABLE = 0o755
"""Stored mode for a member git records as ``100755``."""

MODE_PLAIN = 0o644
"""Stored mode for everything else, including a member absent from the index."""

DEFAULT_OUT = CONNECTOR_ROOT / "dist"
"""Gitignored build output directory (see ``.gitignore``: dist/)."""

FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
"""The zip epoch. Earliest value the format can store; any constant would do.

What matters is that it is a CONSTANT: it is what makes two builds of one
commit byte-identical.
"""


def _load_rules() -> dict:
    """Read and validate ``tiers.json``, or raise for the exit-3 path."""
    with RULES_PATH.open(encoding="utf-8") as handle:
        rules = json.load(handle)
    for key in ("ship", "never_ship", "tiers"):
        if key not in rules:
            raise KeyError(f"{RULES_PATH} is missing required key {key!r}")
    return rules


def _matches(member: str, pattern: str) -> bool:
    """Match ``member`` against a glob where a leading ``**/`` may span zero dirs.

    Mirrors ``assert_zip._matches`` exactly, and for the same reason: ``fnmatch``
    reads ``**/GOTCHAS*`` as requiring at least one directory component, so a
    root-level ``GOTCHAS.md`` would be packed by a builder that trusted the
    library and then refused by the judge that does not. Two different readings
    of one manifest is worse than either reading.
    """
    if fnmatch.fnmatch(member, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(member, pattern[3:])
    return False


def _refused(member: str, patterns: list[str]) -> bool:
    """True when any pattern claims this member."""
    return any(_matches(member, pattern) for pattern in patterns)


def _tracked_files() -> set[str]:
    """Every git-TRACKED path under the connector, relative arcname form.

    The build ships tracked files ONLY. The working tree also holds gitignored
    regenerables (the 2.8 MB voice corpus), other lanes' untracked leftovers
    (#101), and local dev state -- all of which walked straight into the first
    build, because ``rglob`` cannot tell a shipped source file from a stray.
    Git already maintains exactly the set a checkout would produce, so the CI
    build and a local build of the same commit agree by construction.

    A failure here is exit 3, not a silent fallback to the disk walk: a build
    that cannot tell tracked from stray must not guess, because the stray it
    would ship might be a credential.
    """
    try:
        # PATH-resolved `git` on purpose: this runs on dev machines and CI
        # runners whose git lives at different absolute paths; argv is fixed.
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=CONNECTOR_ROOT,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"git ls-files failed under {CONNECTOR_ROOT}: {error}") from error
    return {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}


def _tracked_modes() -> dict[str, int]:
    """Unix permission bits for every tracked path, keyed by arcname.

    Read from the git INDEX (``git ls-files -s`` reports mode ``100755`` or
    ``100644`` per entry), never from the working tree's ``stat``. The index
    is identical on every checkout and on CI, so the artifact's bytes stay a
    function of the commit alone; a working-tree stat varies with umask and
    filesystem, which would make two builds of one commit differ.

    Git stores only the executable bit, so the answer is 0o755 or 0o644 and
    nothing else -- which is exactly the distinction a kit needs.
    """
    try:
        # PATH-resolved `git` for the same reason as _tracked_files; argv fixed.
        result = subprocess.run(
            ["git", "ls-files", "-s", "-z"],
            cwd=CONNECTOR_ROOT,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"git ls-files -s failed under {CONNECTOR_ROOT}: {error}") from error

    modes: dict[str, int] = {}
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        # `<mode> <sha> <stage>\t<path>` -- the path is tab-separated because
        # it may contain spaces; the metadata before it never does.
        meta, _, name = entry.partition(b"\t")
        if not name:
            continue
        modes[name.decode("utf-8")] = (
            MODE_EXECUTABLE if meta.split()[0] == b"100755" else MODE_PLAIN
        )
    return modes


def _candidates(ship: list[str]) -> tuple[list[str], list[str]]:
    """Expand the ship allowlist into (arcnames, missing entries).

    A ship entry naming a directory contributes every file beneath it; one
    naming a file contributes itself. A missing entry is REPORTED rather than
    skipped -- a kit quietly built without ``INSTALL-RUNBOOK.md`` because a
    rename went unnoticed is precisely the failure the allowlist exists to make
    impossible.
    """
    arcnames: list[str] = []
    missing: list[str] = []
    for entry in ship:
        source = CONNECTOR_ROOT / entry
        if not source.exists():
            missing.append(entry)
        elif source.is_dir():
            arcnames.extend(
                path.relative_to(CONNECTOR_ROOT).as_posix()
                for path in source.rglob("*")
                if path.is_file()
            )
        else:
            arcnames.append(entry)
    return arcnames, missing


TESTS_GLOB = "tests/**"
"""The path prefix withheld from the WITHOUT-tests cut of every tier."""

TESTS_NOTE_MEMBER = "TESTS-NOTE.md"
"""Root note the WITH-tests cut carries; see ``TESTS_NOTE_BODY``."""

TIER_MEMBER = "TIER"
"""Root file naming the cut this artifact is, one line, no newline noise.

WHY IT EXISTS: nothing in the source tree records which tier a copy came from,
because the tier is a BUILD OUTPUT (#107) and every tier is built from this one
tree. The installed copy therefore cannot answer "is there a newer build of the
thing I actually am" without being told at build time, and ``update-check``
(#67, D13) asks exactly that. Written here rather than inferred from a filename,
because an unzipped folder gets renamed and the filename is gone.
"""

TESTS_NOTE_BODY = """# About `tests/`

This cut of the kit ships the connector's test suite so you can run it
yourself. The suite is fixtures, not behaviour.

Strings under `tests/` that look like drafted replies, voice or tone samples,
or mail bodies are TEST FIXTURES: fixed inputs and expected outputs used to
check the code, not text the connector will ever send or draft. The connector
cannot send mail at all, and drafting is off unless you turn it on.

If you would rather hand out a kit without the suite, use the cut whose
filename has no `-with-tests` suffix.
"""
"""The note D4 requires, so a reader of the with-tests cut is not misled.

The draft/voice strings under ``tests/`` read like product output when grepped
by a buyer or their security reviewer, and the honest answer -- they are
fixtures -- has to travel INSIDE the artifact, because the person who opens the
zip is not the person who was told.
"""


def _members_for(rules: dict, tier_name: str, *, with_tests: bool = True) -> list[str]:
    """Sorted arcnames that survive ``never_ship``, the tier, and the cut.

    The ``with_tests`` cut is the tier as declared; the without-tests cut is
    the same tier minus ``tests/``. Two cuts per tier is D4 (2026-09-01): the
    operator picks which one a given customer receives, so the build produces both
    rather than the packaging manifest deciding for every customer at once.
    """
    tier = rules["tiers"][tier_name]
    refusals = list(rules["never_ship"]) + list(tier.get("exclude", []))
    if not with_tests:
        refusals.append(TESTS_GLOB)
    arcnames, missing = _candidates(rules["ship"])
    if missing:
        raise FileNotFoundError(f"ship entries absent from the tree: {missing}")
    tracked = _tracked_files()
    return sorted({name for name in arcnames if name in tracked and not _refused(name, refusals)})


def _write_zip(target: Path, members: list[str], *, extra: dict[str, bytes] | None = None) -> None:
    """Write ``members`` into ``target`` with fixed timestamps and stable order.

    ``extra`` carries GENERATED members (arcname -> bytes) that exist only in
    the artifact, currently just the with-tests cut's ``TESTS-NOTE.md``. They
    are written with the same fixed timestamp and plain mode, so a generated
    member does not cost the build its byte-stability.

    Each member's bytes are read and written through an explicit ``ZipInfo``
    rather than ``ZipFile.write``, because the convenience method stamps the
    file's real mtime and there is no argument to stop it. The cost is that
    ``external_attr`` must be set deliberately too: a hardcoded 0o644 shipped
    ``install.sh`` unrunnable, so the mode comes from ``_tracked_modes``.
    """
    modes = _tracked_modes()
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname in members:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = modes.get(arcname, MODE_PLAIN) << 16
            archive.writestr(info, (CONNECTOR_ROOT / arcname).read_bytes())
        for arcname, body in sorted((extra or {}).items()):
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = MODE_PLAIN << 16
            archive.writestr(info, body)


JUDGE_PATH = Path(__file__).resolve().parent / "assert_zip.py"
"""The judge this builder runs over every artifact it writes."""


def run_judge(target: Path, tier: str, *, custom: bool = False, with_tests: bool = False) -> int:
    """Judge ``target`` in place, streaming the judge's own receipt; return its exit.

    Shared with ``build_custom.py`` so there is ONE way a builder judges its
    output. The judge is invoked as a SUBPROCESS rather than imported for the
    reason the tests give: the exit code is the contract, ``scripts/`` is not a
    package, and running the real process is what proves the command printed
    beside it actually works.

    The command is printed first, so the receipt reads as "here is what I ran,
    here is what it said" and an operator can re-run it verbatim.
    """
    flag = (["--custom"] if custom else []) + (["--with-tests"] if with_tests else [])
    printable = " ".join([
        "uv",
        "run",
        "python",
        "scripts/packaging/assert_zip.py",
        *flag,
        str(target),
        tier,
    ])
    print(f"ASSERT WITH: {printable}")
    # sys.executable, not a PATH lookup: the judge must run under the SAME
    # interpreter that is running the builder, or a green receipt could come
    # from a different environment than the one that built the artifact.
    result = subprocess.run(
        [sys.executable, str(JUDGE_PATH), *flag, str(target), tier],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _write_and_judge(
    target: Path, members: list[str], tier_name: str, *, with_tests: bool = False
) -> int:
    """Write one tier's artifact then judge it; return the exit code to propagate.

    The two are one step on purpose (#145): an artifact that has been written
    but not judged is indistinguishable on disk from one that passed, so
    separating them is what let unjudged zips reach clients.
    """
    extra = {TIER_MEMBER: f"{tier_name}\n".encode()}
    if with_tests:
        extra[TESTS_NOTE_MEMBER] = TESTS_NOTE_BODY.encode("utf-8")
    try:
        _write_zip(target, members, extra=extra)
    except OSError as error:
        print(f"harness error: {error}", file=sys.stderr)
        return 3
    print(f"BUILT {target} members={len(members) + len(extra or {})}")
    verdict = run_judge(target, tier_name, with_tests=with_tests)
    if verdict != 0:
        print(f"BUILD REJECTED {target}: judge exit {verdict}", file=sys.stderr)
    return verdict


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the two options this builder takes."""
    parser = argparse.ArgumentParser(description="Build the kit zips per tier.")
    parser.add_argument("--tier", default="all", help="enhanced, basic, or all (default)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Build the requested tiers and print one receipt line per artifact."""
    args = _parse_args(argv)

    try:
        rules = _load_rules()
        version = (CONNECTOR_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, json.JSONDecodeError, KeyError) as error:
        print(f"harness error: {error}", file=sys.stderr)
        return 3

    tiers = list(rules["tiers"]) if args.tier == "all" else [args.tier]
    unknown = [name for name in tiers if name not in rules["tiers"]]
    if unknown:
        print(f"harness error: unknown tier(s) {unknown}", file=sys.stderr)
        return 3

    out_dir = Path(args.out)
    for tier_name in tiers:
        # WITH-tests first, WITHOUT-tests last, deliberately: `scripts/kit-release`
        # reads the LAST `BUILT`/`ASSERT WITH:` line out of the build log to
        # decide which artifact it lands on the share. The cut a customer gets
        # by default is the one without the suite, so that cut has to be the
        # one the log names last.
        for with_tests in (True, False):
            try:
                members = _members_for(rules, tier_name, with_tests=with_tests)
            except FileNotFoundError as error:
                print(f"BUILD FAILED {tier_name}: {error}")
                return 1
            except RuntimeError as error:
                print(f"harness error: {error}", file=sys.stderr)
                return 3
            suffix = "-with-tests" if with_tests else ""
            target = out_dir / f"inbox-zero-connector-{tier_name}-{version}{suffix}.zip"
            verdict = _write_and_judge(target, members, tier_name, with_tests=with_tests)
            if verdict != 0:
                return verdict

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
