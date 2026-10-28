"""Packaging hardening: the blind spots the 0.2.8 ship gauntlet found (#145-#147, #155).

Every test here corresponds to a way the 0.2.8 beta-customer M365 managed-install
artifact passed a judge that should have refused it, or to a tier rule that
could never pass a real build. The gauntlet's own receipt is the anchor: that
zip judged ``OK ... members=217`` while carrying a literal ``{}`` for
``secrets/config.json``, a ``REPLACE-ME`` client id in
``config/connector-config.json``, one client's beta note, and the whole voice
corpus generator.

These drive the judge and the builders as SUBPROCESSES, matching
``test_assert_zip.py`` and ``test_build_custom.py``: ``scripts/`` is not a
package, and the exit code is the contract the packager depends on.

Exit grammar under test: ``0`` pass, ``1`` violation, ``3`` harness error.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

CONNECTOR_ROOT = Path(__file__).resolve().parents[2]
JUDGE = "scripts/packaging/assert_zip.py"
BUILDER = "scripts/packaging/build_zips.py"

RULES = json.loads(
    (CONNECTOR_ROOT / "scripts" / "packaging" / "tiers.json").read_text(encoding="utf-8")
)
TRACKED_CONFIG = json.loads(
    (CONNECTOR_ROOT / "config" / "connector-config.json").read_text(encoding="utf-8")
)

# A config member that satisfies the basic tier's pins. Built from the pins
# themselves rather than hardcoded, so a manifest edit cannot leave this
# fixture asserting a rule that no longer exists.
BASIC_PINS: dict[str, object] = RULES["tiers"]["basic"]["config_pins"]


def _config_from_pins(pins: dict[str, object]) -> str:
    """A minimal connector config JSON satisfying every dotted pin."""
    payload: dict[str, object] = {}
    for dotted, expected in pins.items():
        node = payload
        parts = dotted.split(".")
        for key in parts[:-1]:
            node = node.setdefault(key, {})  # type: ignore[assignment]
        node[parts[-1]] = expected
    return json.dumps(payload, indent=2) + "\n"


FILLED_CONFIG = {
    **TRACKED_CONFIG,
    "credentials": {"google_client_id": "1234-abc.apps.googleusercontent.com"},
}
"""The tracked config with its placeholder resolved, as a CUSTOM build produces.

The committed ``config/connector-config.json`` carries
``REPLACE-ME.apps.googleusercontent.com`` on purpose -- that is correct content
for a shippable kit, and the runbook tells the buyer to replace it. Custom-mode
fixtures therefore cannot reuse it verbatim, or every one of them trips the
very placeholder check under test.
"""

FLOOR = {
    "install.sh": "#!/usr/bin/env bash\necho install\n",
    "install.ps1": "Write-Host install\n",
    "AGENTS.md": "# Agent instructions\n",
    "INSTALL-RUNBOOK.md": "# Install runbook\n",
    "pyproject.toml": '[project]\nname = "inbox-zero-connector"\n',
    "config/connector-config.json": json.dumps(FILLED_CONFIG, indent=2) + "\n",
    "secrets/config.example.json": '{\n  "credentials": {}\n}\n',
}
"""The self-contained floor as of #145, every member present."""

FILLED_SECRETS = '{\n  "credentials": {\n    "classifier_api_key": "sk-not-a-real-key"\n  }\n}\n'


def _make_zip(
    tmp_path: Path,
    members: dict[str, str | bytes],
    name: str = "kit.zip",
    comment: bytes | None = None,
) -> Path:
    """Write ``members`` (arcname -> text or bytes) into a zip and return its path."""
    target = tmp_path / name
    with zipfile.ZipFile(target, "w") as archive:
        if comment is not None:
            archive.comment = comment
        for arcname, body in members.items():
            archive.writestr(arcname, body)
    return target


def _judge(zip_path: Path, tier: str, *, custom: bool = False) -> subprocess.CompletedProcess[str]:
    """Run the packaging judge over ``zip_path`` for ``tier``."""
    flag = ["--custom"] if custom else []
    return subprocess.run(
        ["uv", "run", "python", JUDGE, *flag, str(zip_path), tier],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# #145 -- judge blind spots
# ---------------------------------------------------------------------------


def test_custom_empty_secrets_is_refused_when_undeclared(tmp_path: Path) -> None:
    """``{}`` secrets fail unless the artifact declares itself secretless.

    THE 0.2.8 DEFECT, EXACTLY. ``_unfilled_leaves({})`` is ``[]`` -- there are
    no unfilled leaves -- so a build whose every vault read silently produced
    nothing passed vacuously, and the beta-customer M365 zip shipped ``{}`` with an
    ``OK`` receipt. Emptiness is a legitimate state (the Microsoft public-client
    path needs no secret) but only when somebody SAID so, which is what the
    archive comment records.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["secrets/config.json"] = "{}\n"

    result = _judge(_make_zip(tmp_path, members), "enhanced", custom=True)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "custom_secrets_empty" in result.stdout


def test_custom_empty_secrets_passes_when_declared_secretless(tmp_path: Path) -> None:
    """A DECLARED secretless custom artifact is accepted with empty secrets.

    The declaration is the whole mechanism: it turns "the build produced
    nothing" into "the operator stated there is nothing", which are the two
    readings of ``{}`` a judge otherwise cannot separate.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["secrets/config.json"] = "{}\n"
    marker = b"inbox-zero-connector: profile declared no secrets"

    result = _judge(_make_zip(tmp_path, members, comment=marker), "enhanced", custom=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "VIOLATION" not in result.stdout


def test_custom_config_placeholder_is_refused_without_echoing_value(tmp_path: Path) -> None:
    """A ``REPLACE-ME`` in the CONNECTOR CONFIG fails a custom build.

    The 0.2.8 artifact carried ``REPLACE-ME.apps.googleusercontent.com`` at
    ``credentials.google_client_id`` and judged clean, because the placeholder
    scan only ever opened ``secrets/config.json``. A managed install is defined
    by the client never touching configuration, so an unfilled value here is as
    fatal as one in the secrets.
    """
    config = dict(TRACKED_CONFIG)
    config["credentials"] = {"google_client_id": "REPLACE-ME.apps.googleusercontent.com"}
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = json.dumps(config, indent=2) + "\n"
    members["secrets/config.json"] = FILLED_SECRETS

    result = _judge(_make_zip(tmp_path, members), "enhanced", custom=True)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "credentials.google_client_id" in result.stdout
    assert "REPLACE-ME" not in result.stdout


def test_kit_build_keeps_replace_me_in_config(tmp_path: Path) -> None:
    """A SHIPPABLE kit may carry ``REPLACE-ME``; that is what the runbook says to fix.

    The placeholder scan is scoped to ``--custom`` on purpose. Without this
    test, "scan the config for placeholders" reads as a rule that would also
    make every kit build fail, which would be a worse defect than the one it
    fixes.
    """
    config = dict(TRACKED_CONFIG)
    config["credentials"] = {"google_client_id": "REPLACE-ME.apps.googleusercontent.com"}
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = json.dumps(config, indent=2) + "\n"

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 0, result.stdout + result.stderr


def test_empty_string_in_config_is_not_a_placeholder(tmp_path: Path) -> None:
    """``folder_prefix: ""`` is real configuration, not an unfilled credential.

    The secrets scan treats an empty string as unfilled, correctly -- every
    leaf there is a credential. The connector config is not that: an empty
    string is the shipped default for ``folder_prefix``, so reusing the secrets
    rule verbatim would fail every honest custom build.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = (
        json.dumps({**FILLED_CONFIG, "folder_prefix": ""}, indent=2) + "\n"
    )
    members["secrets/config.json"] = FILLED_SECRETS

    result = _judge(_make_zip(tmp_path, members), "enhanced", custom=True)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "member",
    ["install.ps1", "config/connector-config.json", "secrets/config.example.json"],
)
def test_floor_grew_to_cover_install_blocking_absences(tmp_path: Path, member: str) -> None:
    """Each member added to the floor in #145 is individually required.

    Parametrized rather than asserted in one zip so a regression names WHICH
    floor member stopped being checked; a single combined artifact would go
    green the moment any one of the three was still enforced.
    """
    members = {k: v for k, v in FLOOR.items() if k != member}

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"missing_floor {member}" in result.stdout


@pytest.mark.parametrize("suffix", [".sh", ".ps1", ".json", ".toml"])
def test_text_scan_reaches_the_new_suffixes(tmp_path: Path, suffix: str) -> None:
    """Content matching opens installers, config, and metadata -- not only .py/.md.

    ``TEXT_SUFFIXES`` was ``(".py", ".md")``, so a forbidden reference sitting
    in ``install.ps1`` or a shipped ``.json`` was never read. Driven through the
    BASIC tier's real ``forbidden_content`` so the test asserts the shipped
    manifest rather than a rule invented here.
    """
    forbidden = RULES["tiers"]["basic"]["forbidden_content"][0]
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = _config_from_pins(BASIC_PINS)
    members[f"docs/leak{suffix}"] = f"reference to {forbidden}\n"

    result = _judge(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"forbidden_content docs/leak{suffix}" in result.stdout


def test_undecodable_text_member_is_a_violation_not_a_skip(tmp_path: Path) -> None:
    """Non-UTF-8 bytes under a text suffix fail; they used to be skipped green.

    The old behaviour printed ``SKIP undecodable text member`` and returned no
    finding, which means a member the manifest says is scannable could opt
    itself out of the scan by not being decodable. For a containment check that
    is the wrong default: the artifact is not what the manifest claims.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["src/inbox_zero_connector/blob.py"] = b"\xff\xfe\x00not utf-8"

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "undecodable_text_member src/inbox_zero_connector/blob.py" in result.stdout


# ---------------------------------------------------------------------------
# #146 -- the basic tier's capability assertion
# ---------------------------------------------------------------------------


def test_basic_tier_requires_the_file_only_posture_pins(tmp_path: Path) -> None:
    """The basic artifact must PIN file-only posture with drafting off.

    This is the assertion that replaced the substring scan. ``create_draft``
    appears in the audit ledger and the provider protocol, which the basic tier
    legitimately ships, so matching on it produced 63 violations against a
    correct build and the SKU could only ship by skipping the judge. What
    actually distinguishes the tier is the posture the shipped config pins --
    a fact in the bytes, and one the buyer can check.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = json.dumps(
        {"posture": "file_read_draft", "drafts_enabled": {"google": True, "microsoft": True}},
        indent=2,
    )

    result = _judge(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "config_pin" in result.stdout
    assert "posture" in result.stdout


def test_basic_tier_accepts_a_correctly_pinned_config(tmp_path: Path) -> None:
    """The pins are satisfiable: a correctly-pinned basic artifact passes.

    A rule nothing can satisfy is #146 all over again, so the green case is
    asserted beside the red one.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = _config_from_pins(BASIC_PINS)

    result = _judge(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 0, result.stdout + result.stderr


def test_config_pin_on_an_absent_key_is_a_violation(tmp_path: Path) -> None:
    """A config that simply omits a pinned key fails rather than passing.

    Absence is the failure mode a pin most needs to catch: a config without
    ``posture`` falls back to whatever the code defaults to, which is exactly
    the implicit behaviour the tier is sold as not having.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = "{}\n"

    result = _judge(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "config_pin_missing" in result.stdout


def test_enhanced_tier_has_no_pins_so_any_posture_passes(tmp_path: Path) -> None:
    """Pins are per-tier data; the enhanced tier states none and constrains nothing."""
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = json.dumps(
        {"posture": "file_read_draft", "drafts_enabled": {"google": True}}, indent=2
    )

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 0, result.stdout + result.stderr


def test_basic_forbidden_content_targets_the_excluded_module_path(tmp_path: Path) -> None:
    """The surviving content rule names the excluded members' OWN module path.

    Path exclusion alone still is not enough -- a helper MOVED out of
    ``services/voice/`` carries the capability with it -- so the content rule
    was narrowed to the module path rather than deleted.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = _config_from_pins(BASIC_PINS)
    members["src/inbox_zero_connector/services/triage/helper.py"] = (
        "from inbox_zero_connector.services.voice import drafting\n"
    )

    result = _judge(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "forbidden_content" in result.stdout


def test_ledger_style_create_draft_mention_no_longer_fails_basic(tmp_path: Path) -> None:
    """A bare ``create_draft`` mention is NOT a basic-tier violation any more.

    This is the #146 regression guard. The audit ledger, the provider protocol,
    and the security-surface tests all name ``create_draft`` and all ship in
    basic; a rule that refused them made the tier unbuildable-as-judged.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["config/connector-config.json"] = _config_from_pins(BASIC_PINS)
    members["src/inbox_zero_connector/services/audit/ledger.py"] = (
        'WRITE_METHODS = ("file_message", "create_draft")\n'
    )

    result = _judge(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# #147 / #155 -- ship-list corrections
# ---------------------------------------------------------------------------


def test_client_beta_note_never_ships(tmp_path: Path) -> None:
    """One client's beta note is refused in every tier (#147).

    It sat in the ``ship`` allowlist unconditionally, so names, domains, and
    tenant status reached every other buyer's artifact. It is delivered outside
    the zip now, and the ``never_ship`` entry is what makes a re-add fail.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members["CLIENT-BETA-NOTE.md"] = "client specific beta facts\n"

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "CLIENT-BETA-NOTE.md" in result.stdout


@pytest.mark.parametrize(
    "member",
    [
        "scripts/voice_corpus/generate.py",
        "scripts/voice_corpus/PERSONAS.md",
        "scripts/voice_corpus/corpus/sample.json",
    ],
)
def test_voice_corpus_tree_never_ships(tmp_path: Path, member: str) -> None:
    """The whole ``voice_corpus`` tree is refused, not just its ``corpus/`` (#155).

    ``never_ship`` carried ``scripts/voice_corpus/corpus/**`` only, so the
    generator and its internal findings docs -- 10 members -- shipped in every
    client zip.
    """
    members: dict[str, str | bytes] = dict(FLOOR)
    members[member] = "internal generator\n"

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert member in result.stdout


def test_ship_list_no_longer_names_the_client_beta_note() -> None:
    """The manifest itself is asserted, not only the judge's behaviour.

    The judge refusing the member and the builder still trying to pack it would
    be a build that fails every time -- correct but useless. The ship list has
    to have dropped it too.
    """
    assert "CLIENT-BETA-NOTE.md" not in RULES["ship"]


# ---------------------------------------------------------------------------
# The builders judge their own output (#145)
# ---------------------------------------------------------------------------


def _git_work_tree() -> bool:
    """Whether this is a real git work tree of the connector.

    The builders ship git-TRACKED members, so a checkout-index copy (what the
    pre-commit hook runs in) has an empty member list by construction and these
    tests would report a defect that does not exist.
    """
    git = shutil.which("git")
    if git is None:
        return False
    probe = subprocess.run(
        [git, "ls-files", "--", "config/connector-config.json"],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "config/connector-config.json"


NEEDS_WORK_TREE = pytest.mark.skipif(
    not _git_work_tree(),
    reason="the builders ship git-tracked members; this is not a connector work tree",
)


@NEEDS_WORK_TREE
def test_build_zips_runs_its_own_judge_and_still_prints_the_command(tmp_path: Path) -> None:
    """The kit builder RUNS the judge over what it wrote, and says what it ran.

    It used to only print the command, which made an unjudged zip
    indistinguishable on disk from a judged one -- so the artifact most likely
    to reach a client was the one built by whoever skipped the second step. The
    printed line survives, because a receipt an operator can re-run is what
    makes the verdict checkable by somebody who was not there.
    """
    result = subprocess.run(
        ["uv", "run", "python", BUILDER, "--tier", "enhanced", "--out", str(tmp_path)],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ASSERT WITH:" in result.stdout, result.stdout
    # The judge's OWN receipt line -- proof the process ran, not just that the
    # command was printed.
    assert "mode=kit tier=enhanced" in result.stdout, result.stdout


@NEEDS_WORK_TREE
def test_enhanced_tier_build_ships_no_client_note_and_no_corpus(tmp_path: Path) -> None:
    """A real enhanced build carries neither the client note nor the corpus tree.

    The end-to-end check for #147 and #155 together: the manifest edits are
    only worth anything if the artifact they produce actually lacks the
    members, which is a fact about the built zip rather than about the JSON.
    """
    result = subprocess.run(
        ["uv", "run", "python", BUILDER, "--tier", "enhanced", "--out", str(tmp_path)],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # BOTH D4 cuts, because "the corpus never ships" is a claim about every
    # artifact the build writes, not about whichever one happens to be first.
    built = sorted(tmp_path.glob("*.zip"))
    assert len(built) == 2, [p.name for p in built]
    for artifact in built:
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()

        assert "CLIENT-BETA-NOTE.md" not in names, artifact.name
        assert [n for n in names if n.startswith("scripts/voice_corpus/")] == [], artifact.name


# ---------------------------------------------------------------------------
# #58 / D4 -- two cuts per tier
# ---------------------------------------------------------------------------


@NEEDS_WORK_TREE
def test_tier_build_produces_both_cuts_and_only_one_carries_tests(tmp_path: Path) -> None:
    """One tier build writes both D4 cuts, each judged as the cut it is.

    The plain zip must carry no ``tests/`` at all; the ``-with-tests`` zip must
    carry the suite AND the generated fixture note, because a buyer grepping
    the suite finds drafted-looking strings and the honest answer has to travel
    inside the artifact.
    """
    result = subprocess.run(
        ["uv", "run", "python", BUILDER, "--tier", "enhanced", "--out", str(tmp_path)],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    built = {p.name: p for p in tmp_path.glob("*.zip")}
    with_tests = [name for name in built if name.endswith("-with-tests.zip")]
    plain = [name for name in built if not name.endswith("-with-tests.zip")]
    assert len(with_tests) == 1 and len(plain) == 1, sorted(built)

    with zipfile.ZipFile(built[plain[0]]) as archive:
        plain_names = archive.namelist()
    with zipfile.ZipFile(built[with_tests[0]]) as archive:
        tested_names = archive.namelist()
        note = archive.read("TESTS-NOTE.md").decode("utf-8")

    assert [n for n in plain_names if n.startswith("tests/")] == []
    assert "TESTS-NOTE.md" not in plain_names
    assert [n for n in tested_names if n.startswith("tests/")] != []
    assert "fixture" in note.lower()

    # Both judge verdicts are in the builder's own receipt, so the cut is
    # proven by the judge that ran, not only by this test's own reading.
    assert "cut=no-tests" in result.stdout, result.stdout
    assert "cut=with-tests" in result.stdout, result.stdout


def test_plain_cut_carrying_tests_is_refused(tmp_path: Path) -> None:
    """The judge reads the BYTES, not the filename: tests in a plain cut fail."""
    zip_path = _make_zip(tmp_path, {**FLOOR, "tests/test_thing.py": "def test_x(): pass\n"})
    result = _judge(zip_path, "enhanced")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "VIOLATION tests_in_plain_cut" in result.stdout, result.stdout
