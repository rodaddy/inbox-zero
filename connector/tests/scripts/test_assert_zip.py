"""Packaging judge: what may physically enter a shipped zip, per tier.

``scripts/packaging/assert_zip.py`` is the deterministic answer to the #107
no-config-gating ruling: the BASIC artifact must physically LACK the drafting
intelligence, not merely have it switched off behind a flag. A config toggle
is a promise; a zip that does not contain ``create_draft`` at all is a fact,
and a buyer with ``unzip`` can check the fact themselves.

These tests exercise the judge the same way ``test_containment.py`` exercises
the source scanner -- by building an artifact that SHOULD fail and proving the
check actually fails on it. A judge nobody has seen go red is not evidence of
anything, so every violation class here gets its own zip.

Two rulings are asserted through the ``never_ship`` list rather than restated:
internal process files (``_DOCS/``, ``HANDOFF-RULES.md``, ``GOTCHAS*``,
``_graph/``) never enter a shipped zip (decision, 2026-08-21), and the real
``secrets/config.json`` never ships at all.

The judge is driven as a SUBPROCESS rather than imported. ``scripts/`` is not
a package and has no import path into the test suite; the exit code IS the
contract the caller depends on, so testing it through the process boundary
tests the thing that actually ships instead of an internal function that
happens to sit behind it.

Exit grammar under test: ``0`` pass, ``1`` violation, ``3`` harness error.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

# The connector root -- three parents up from tests/scripts/<this file>.
# The judge reads ``tiers.json`` from beside itself, so it must be invoked
# with the repo root as cwd, exactly as a packaging run would invoke it.
CONNECTOR_ROOT = Path(__file__).resolve().parents[2]
JUDGE = "scripts/packaging/assert_zip.py"

# The self-contained floor: an artifact missing any of these cannot be
# installed by the MSP who receives it, so its absence is a violation in
# every tier. ``config.py`` stands in for real shipped source.
FLOOR = {
    "install.sh": "#!/usr/bin/env bash\necho install\n",
    # `install.ps1`, the connector config, and the secrets example joined the
    # floor in #145: each is install-blocking by its absence, and each absence
    # used to surface on the client's machine rather than at package time.
    "install.ps1": "Write-Host install\n",
    "AGENTS.md": "# Agent instructions\n",
    "INSTALL-RUNBOOK.md": "# Install runbook\n",
    "pyproject.toml": '[project]\nname = "inbox-zero-connector"\n',
    "config/connector-config.json": '{\n  "posture": "file_only"\n}\n',
    "secrets/config.example.json": '{\n  "credentials": {}\n}\n',
    "src/inbox_zero_connector/config.py": "SETTINGS = {}\n",
}


def _make_zip(tmp_path: Path, members: dict[str, str], name: str = "kit.zip") -> Path:
    """Write ``members`` (arcname -> text) into a zip and return its path."""
    target = tmp_path / name
    with zipfile.ZipFile(target, "w") as archive:
        for arcname, text in members.items():
            archive.writestr(arcname, text)
    return target


def _judge(zip_path: Path, tier: str) -> subprocess.CompletedProcess[str]:
    """Run the packaging judge over ``zip_path`` for ``tier``."""
    return subprocess.run(
        ["uv", "run", "python", JUDGE, str(zip_path), tier],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_enhanced_zip_passes(tmp_path: Path) -> None:
    """A minimal, correct FULL artifact is accepted with an empty receipt."""
    result = _judge(_make_zip(tmp_path, dict(FLOOR)), "enhanced")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "VIOLATION" not in result.stdout


def test_never_ship_member_fails(tmp_path: Path) -> None:
    """Internal process files are refused in EVERY tier, and each is named.

    Both members are present at once because a judge that stops at the first
    violation gives a packager one round trip per mistake.
    """
    members = dict(FLOOR)
    members["src/inbox_zero_connector/_DOCS/notes.md"] = "internal notes\n"
    members["HANDOFF-RULES.md"] = "internal handoff rules\n"

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/inbox_zero_connector/_DOCS/notes.md" in result.stdout
    assert "HANDOFF-RULES.md" in result.stdout


def test_basic_tier_excluded_path_fails(tmp_path: Path) -> None:
    """The voice package is a PATH the basic artifact may not contain."""
    members = dict(FLOOR)
    members["src/inbox_zero_connector/services/voice/drafting.py"] = "def draft(): ...\n"

    result = _judge(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/inbox_zero_connector/services/voice/drafting.py" in result.stdout


def test_basic_tier_forbidden_content_fails(tmp_path: Path) -> None:
    """Drafting capability is refused by CONTENT, not only by path.

    A file relocated out of ``services/voice/`` still carries the intelligence
    the basic tier is sold as lacking, so the same zip must fail as ``basic``
    and pass as ``enhanced``.

    The refused substring is the excluded package's own MODULE PATH, not
    ``create_draft``. #146 proved the latter cannot work: ``create_draft``
    names a provider method the audit ledger and the security-surface tests
    both reference, and both ship in basic, so the rule produced 63 violations
    against a correct build. The capability question the tier is actually sold
    on is answered by ``config_pins`` instead (see
    ``test_packaging_hardening.py``); this rule now catches only the narrower
    thing it can catch honestly -- a shipped member reaching into the package
    that was withheld.
    """
    members = dict(FLOOR)
    members["src/inbox_zero_connector/services/triage/helper.py"] = (
        "from inbox_zero_connector.services.voice import drafting\n"
    )
    zip_path = _make_zip(tmp_path, members)

    basic = _judge(zip_path, "basic")
    assert basic.returncode == 1, basic.stdout + basic.stderr
    assert "forbidden_content" in basic.stdout

    enhanced = _judge(zip_path, "enhanced")
    assert enhanced.returncode == 0, enhanced.stdout + enhanced.stderr


def test_missing_floor_file_fails(tmp_path: Path) -> None:
    """An artifact without ``install.sh`` cannot be installed, so it fails."""
    members = {k: v for k, v in FLOOR.items() if k != "install.sh"}

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "install.sh" in result.stdout


def test_pycache_refused_everywhere(tmp_path: Path) -> None:
    """Build residue is refused in every tier, wherever it sits in the tree."""
    members = dict(FLOOR)
    members["src/__pycache__/x.pyc"] = "compiled residue\n"

    result = _judge(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/__pycache__/x.pyc" in result.stdout


# ---------------------------------------------------------------------------
# Custom-build mode (``--custom``)
#
# A custom zip is built by ``build_custom.py`` for a managed install and
# CONTAINS ``secrets/config.json`` on purpose -- that member is the whole
# reason the client never touches configuration. The shipped-kit judge refuses
# that member in every tier, correctly, so a custom artifact needs its own
# mode rather than a hole punched in ``never_ship``: the exemption must apply
# to the ONE artifact class that earns it and to nothing else.
#
# The mode is a FLAG rather than a tier name because the tier is orthogonal.
# A custom build still cuts ``enhanced`` or ``basic`` (the profile's ``zip_tier``),
# and every tier rule still applies to it; "custom" describes how the artifact
# was produced, not what capability it carries. A ``"custom"`` entry in
# ``tiers.json`` would have to duplicate whichever real tier it wrapped.
# ---------------------------------------------------------------------------

VALID_SECRETS = (
    '{\n  "credentials": {\n'
    '    "google_client_id": "1234-abc.apps.googleusercontent.com",\n'
    '    "google_client_secret": "notarealsecret"\n'
    "  }\n}\n"
)


def _judge_custom(zip_path: Path, tier: str) -> subprocess.CompletedProcess[str]:
    """Run the judge in custom-build mode over ``zip_path`` for ``tier``."""
    return subprocess.run(
        ["uv", "run", "python", JUDGE, "--custom", str(zip_path), tier],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_custom_mode_allows_filled_secrets_config(tmp_path: Path) -> None:
    """The one member a custom artifact MUST carry is accepted in custom mode.

    The same zip is refused by the default mode, which is what proves the
    exemption is scoped to the flag rather than removed from the rules.
    """
    members = dict(FLOOR)
    members["secrets/config.json"] = VALID_SECRETS
    zip_path = _make_zip(tmp_path, members)

    custom = _judge_custom(zip_path, "enhanced")
    assert custom.returncode == 0, custom.stdout + custom.stderr
    assert "VIOLATION" not in custom.stdout

    shipped = _judge(zip_path, "enhanced")
    assert shipped.returncode == 1, shipped.stdout + shipped.stderr
    assert "secrets/config.json" in shipped.stdout


def test_custom_mode_refuses_placeholder_without_echoing_value(tmp_path: Path) -> None:
    """An unfilled credential fails, and the receipt names the KEY PATH only.

    A custom zip whose ``secrets/config.json`` still says ``REPLACE-ME`` is a
    build that silently did not resolve -- the client installs it and the
    connector cannot authenticate. The violation therefore has to be loud.

    It also has to be quiet about VALUES. This judge runs in packaging output
    that lands in terminals, CI logs, and handoff notes, so a message quoting
    the offending leaf would put a live credential wherever that output went.
    The key path is enough to fix the profile and carries nothing secret.
    """
    members = dict(FLOOR)
    members["secrets/config.json"] = (
        '{\n  "credentials": {\n'
        '    "google_client_id": "REPLACE-ME",\n'
        '    "google_client_secret": "s3cr3t-must-not-appear"\n'
        "  }\n}\n"
    )

    result = _judge_custom(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "credentials.google_client_id" in result.stdout
    assert "REPLACE-ME" not in result.stdout
    assert "s3cr3t-must-not-appear" not in result.stdout


def test_custom_mode_refuses_empty_and_tbd_leaves(tmp_path: Path) -> None:
    """``TBD`` and the empty string are unfilled too, and each is named.

    ``TBD-#NN`` is the convention ``profiles/README.md`` documents for a value
    blocked on a GitHub issue, so it reaches a built zip exactly when someone
    builds before the issue lands. An empty string is the same defect arriving
    from a vault field that resolved to nothing.
    """
    members = dict(FLOOR)
    members["secrets/config.json"] = (
        '{\n  "credentials": {\n'
        '    "google_client_id": "TBD-#118",\n'
        '    "classifier_api_key": ""\n'
        "  }\n}\n"
    )

    result = _judge_custom(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "credentials.google_client_id" in result.stdout
    assert "credentials.classifier_api_key" in result.stdout


def test_custom_mode_refuses_unparseable_secrets_config(tmp_path: Path) -> None:
    """A ``secrets/config.json`` that is not JSON is a violation, not a crash."""
    members = dict(FLOOR)
    members["secrets/config.json"] = "{not json\n"

    result = _judge_custom(_make_zip(tmp_path, members), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "secrets/config.json" in result.stdout


def test_custom_mode_still_applies_tier_rules(tmp_path: Path) -> None:
    """Custom mode exempts ONE member; every other rule is untouched.

    Both a ``never_ship`` member and a basic-tier content leak are present, so
    a mode that had been implemented as "skip the checks for custom builds"
    fails here instead of passing quietly.
    """
    members = dict(FLOOR)
    members["secrets/config.json"] = VALID_SECRETS
    members["HANDOFF-RULES.md"] = "internal handoff rules\n"
    members["src/inbox_zero_connector/services/triage/helper.py"] = (
        "from inbox_zero_connector.services.voice import drafting\n"
    )

    result = _judge_custom(_make_zip(tmp_path, members), "basic")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "HANDOFF-RULES.md" in result.stdout
    assert "forbidden_content" in result.stdout


def test_custom_mode_requires_the_secrets_member(tmp_path: Path) -> None:
    """A custom build with no credentials inside it is not a custom build."""
    result = _judge_custom(_make_zip(tmp_path, dict(FLOOR)), "enhanced")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "secrets/config.json" in result.stdout
