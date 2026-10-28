"""Custom-build contract: one client's zip, their config merged, secrets resolved.

``scripts/packaging/build_custom.py`` is the determinism-first answer to the
per-client custom zip. A build for a named client is a profile JSON plus a
vault read, never a hand-edited working tree: the profile names WHICH vault
entry holds each secret, the builder resolves it at package time, and the
resulting zip is the only place a resolved value ever exists.

The refusals are the point, and each gets its own test here:

* A **string leaf** under ``secrets`` means somebody pasted a value into a
  profile. Profiles are checked in, reviewed, and pasted between machines, so
  a value in one is a leaked credential by construction. Exit 1.
* A **failed vault read** names the ENTRY, never the field's value. A message
  that echoes what it just read defeats the whole indirection.
* An ``--out`` inside the connector root would drop a client's resolved
  secrets into the working tree, where the next ``git add`` picks them up.

The builder is driven as a SUBPROCESS rather than imported, for the same
reason ``test_assert_zip.py`` drives the judge that way: ``scripts/`` is not a
package and has no import path into the suite, and the exit code IS the
contract the packager depends on.

The vault is faked with a script on disk rather than mocked, because
``--vault-cmd`` is an argv contract -- shlex-split, two appended elements,
JSON on stdout -- and a mock would assert the shape the test itself invented
instead of the shape a real vault CLI is handed.

Exit grammar under test: ``0`` built, ``1`` refused, ``3`` harness error.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- the script IS the thing under test.
import sys
import zipfile
from pathlib import Path

import pytest

# The connector root -- two parents up from tests/scripts/<this file>. The
# builder reads VERSION, tiers.json, and the tracked config from beside
# itself, so it must be invoked with the repo root as cwd, exactly as a
# packaging run would invoke it.
CONNECTOR_ROOT = Path(__file__).resolve().parents[2]
BUILDER = "scripts/packaging/build_custom.py"

VERSION = (CONNECTOR_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _git_sees_the_tracked_config() -> bool:
    """Whether ``git ls-files`` from the connector root reports the tracked config.

    The builder ships git-TRACKED members only (``build_zips._tracked_files``),
    so the happy path needs a real work tree. The pre-commit hook runs this
    suite inside a ``git checkout-index`` copy under ``.git/``, which is not
    one: there the member list is empty by construction and the test would
    report a builder defect that does not exist. The precondition is checked
    rather than assumed, and the skip names why.
    """
    git = shutil.which("git")
    if git is None:
        return False
    probe = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]  (fixed argv, shell=False)
        [git, "ls-files", "--", "config/connector-config.json"],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "config/connector-config.json"


NEEDS_WORK_TREE = pytest.mark.skipif(
    not _git_sees_the_tracked_config(),
    reason="builder ships git-tracked members; this tree is not a git work tree of the connector",
)
TRACKED_CONFIG = json.loads(
    (CONNECTOR_ROOT / "config" / "connector-config.json").read_text(encoding="utf-8")
)

# The marker every fake vault read returns. It must never reach stdout or a
# log line; asserting on this exact string is how "never prints a secret" is
# checked mechanically rather than by reading the source.
FAKE_PREFIX = "sk-FAKE-"

# A fake vault CLI. It parses the `--params` JSON argv element the builder
# appends and answers in the shape a real vault client returns, so the test
# exercises the argv contract instead of a stub of it.
FAKE_VAULT = """\
import json
import sys

params = json.loads(sys.argv[sys.argv.index("--params") + 1])
assert "field" not in params, "builder must fetch the whole entry, not a field"
print(json.dumps({"success": True, "result": {
    "name": params["query"],
    "value": "sk-FAKE-value",
    "fields": {"password": "sk-FAKE-password", "notes": "not a secret"},
}}))
"""

# A vault that fails the way a real one does when the entry is missing: a
# nonzero exit and nothing at all on stdout.
FAILING_VAULT = """\
import sys

sys.exit(1)
"""

HAPPY_CONFIG = {
    "voice_routing": {"owner_domain": "example-client.com"},
    "logging": {"level": "DEBUG"},
    "classifier": {
        "base_url": "https://example.invalid/v1",
        "model": "m",
        "requires_key": True,
    },
    # Resolved rather than inherited from the tracked config, which carries a
    # literal `REPLACE-ME` client id. That placeholder is correct content for a
    # shippable KIT and fatal for a managed install, so `assert_zip --custom`
    # refuses it (#145) -- and the builder now runs that judge itself, so a
    # profile that leaves it unfilled cannot build at all. A real profile fills
    # it the same way.
    "credentials": {"google_client_id": "1234-abc.apps.googleusercontent.com"},
}
HAPPY_SECRETS = {"credentials": {"classifier_api_key": {"vault": "entry-x", "field": "password"}}}


def _write_vault(tmp_path: Path, source: str, name: str) -> Path:
    """Write a fake vault CLI to disk and return its path."""
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return target


def _write_profile(tmp_path: Path, profile: dict[str, object], name: str = "profile.json") -> Path:
    """Write a build profile to disk and return its path."""
    target = tmp_path / name
    target.write_text(json.dumps(profile), encoding="utf-8")
    return target


def _build(
    profile: Path, out_dir: Path, vault_cmd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the custom builder over ``profile``, writing into ``out_dir``."""
    argv = [sys.executable, BUILDER, "--profile", str(profile), "--out", str(out_dir)]
    if vault_cmd is not None:
        argv += ["--vault-cmd", vault_cmd]
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, no shell.
        argv,
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _zips(directory: Path) -> list[Path]:
    """Every zip sitting in ``directory``, or an empty list if it does not exist."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.zip"))


def _read_artifact(zip_path: Path) -> tuple[list[str], dict, dict]:
    """The member list, merged config, and resolved secrets inside ``zip_path``."""
    with zipfile.ZipFile(zip_path) as archive:
        return (
            archive.namelist(),
            json.loads(archive.read("config/connector-config.json")),
            json.loads(archive.read("secrets/config.json")),
        )


@NEEDS_WORK_TREE
def test_custom_build_merges_config_and_resolves_secrets(tmp_path: Path) -> None:
    """The happy path: one zip, config deep-merged, secrets resolved from the vault.

    This asserts the full artifact contract at once because the members, the
    merge, and the resolved secrets are one deliverable -- a zip with a correct
    config and no ``secrets/config.json`` is not a partially good build, it is
    an unusable one.
    """
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-test",
            "zip_tier": "enhanced",
            "config": HAPPY_CONFIG,
            "secrets": HAPPY_SECRETS,
        },
    )
    out_dir = tmp_path / "out"

    result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {fake}")

    assert result.returncode == 0, result.stdout + result.stderr

    built = _zips(out_dir)
    assert len(built) == 1, [p.name for p in built]
    assert built[0].name == f"inbox-zero-connector-acme-test-{VERSION}.zip"

    names, merged, secrets = _read_artifact(built[0])

    assert "config/connector-config.json" in names
    assert "secrets/config.json" in names
    assert "install.sh" in names

    # The profile's own values landed...
    assert merged["voice_routing"]["owner_domain"] == "example-client.com"
    assert merged["logging"]["level"] == "DEBUG"
    assert merged["classifier"]["base_url"] == "https://example.invalid/v1"
    # ...and a key the profile never mentioned survived the merge untouched.
    # A shallow overwrite of `voice_routing` or a whole-file replacement both
    # pass the three assertions above and fail this one.
    assert merged["posture"] == TRACKED_CONFIG["posture"]
    assert (
        merged["voice_routing"]["default_voice"]
        == (TRACKED_CONFIG["voice_routing"]["default_voice"])
    )

    assert secrets["credentials"]["classifier_api_key"] == f"{FAKE_PREFIX}password"

    # A `BUILT ` LINE, not a `BUILT ` prefix on stdout: the builder may emit a
    # dirty-member warning first (the zip carries HEAD's bytes, so the operator
    # has to be told their working-tree edits are not in it), and the receipt
    # is a line in the stream rather than the whole of it.
    assert [line for line in result.stdout.splitlines() if line.startswith("BUILT ")]
    assert FAKE_PREFIX not in result.stdout
    assert FAKE_PREFIX not in result.stderr

    # The member count the packager is handed must describe the artifact it
    # was handed, not the builder's intent.
    reported = int(result.stdout.split("members=")[1].split()[0])
    assert reported == len(names)


def _load_builder_module():
    """Import ``build_custom`` as a module so its helpers can be unit-tested.

    ``scripts/`` is not a package, so there is no import path into the suite;
    every other test here therefore drives the script as a subprocess. The
    splitter is the one exception: its whole contract is what it does under a
    platform this test is NOT running on, which a subprocess cannot exercise.
    """
    packaging = CONNECTOR_ROOT / "scripts" / "packaging"
    spec = importlib.util.spec_from_file_location(
        "build_custom_under_test", packaging / "build_custom.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(packaging))  # the module imports its sibling build_zips
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(packaging))
    return module


def test_windows_vault_cmd_keeps_its_backslashes() -> None:
    """A Windows ``--vault-cmd`` survives the split with its path separators.

    ``shlex.split`` defaults to POSIX rules, where ``\\`` is an escape rather
    than a separator, so ``C:\\Py\\python.exe`` splits to ``C:Pypython.exe`` --
    a path that does not exist. The builder then reports the entry as
    unresolvable, which points the operator at the vault instead of at the
    split. Asserted with an explicit ``posix=False`` so a POSIX host proves the
    Windows behaviour.
    """
    module = _load_builder_module()
    argv = module.split_vault_cmd(r"C:\Py\python.exe C:\Temp\vault.py", posix=False)
    assert argv == [r"C:\Py\python.exe", r"C:\Temp\vault.py"]


def test_posix_vault_cmd_still_splits_posix_style() -> None:
    """The Windows fix does not change what a POSIX command line means."""
    module = _load_builder_module()
    assert module.split_vault_cmd("/usr/bin/vault get --json", posix=True) == [
        "/usr/bin/vault",
        "get",
        "--json",
    ]


def test_string_secret_leaf_is_refused(tmp_path: Path) -> None:
    """A pasted value under ``secrets`` is refused: profiles hold references only."""
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-test",
            "zip_tier": "enhanced",
            "config": {},
            "secrets": {"credentials": {"classifier_api_key": "sk-PASTED-BY-HAND"}},
        },
    )
    out_dir = tmp_path / "out"

    result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {fake}")

    assert result.returncode == 1, result.stdout + result.stderr
    assert _zips(out_dir) == []


def test_out_inside_connector_root_is_refused(tmp_path: Path) -> None:
    """An ``--out`` under the connector root would stage secrets into the tree."""
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-test",
            "zip_tier": "enhanced",
            "config": HAPPY_CONFIG,
            "secrets": HAPPY_SECRETS,
        },
    )
    out_dir = CONNECTOR_ROOT / "dist" / "custom-test"

    result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {fake}")

    # A REFUSAL (1), not merely "nonzero": an absent or crashing builder also
    # exits nonzero, so `!= 0` would pass without the guard existing at all.
    assert result.returncode == 1, result.stdout + result.stderr
    assert _zips(out_dir) == []


def test_failed_vault_read_names_the_entry_only(tmp_path: Path) -> None:
    """A vault failure names the ENTRY, builds nothing, and leaks no value."""
    failing = _write_vault(tmp_path, FAILING_VAULT, "vault_fail.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-test",
            "zip_tier": "enhanced",
            "config": HAPPY_CONFIG,
            "secrets": HAPPY_SECRETS,
        },
    )
    out_dir = tmp_path / "out"

    result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {failing}")
    combined = result.stdout + result.stderr

    assert result.returncode == 1, combined
    assert _zips(out_dir) == []
    assert "entry-x" in combined
    assert FAKE_PREFIX not in combined


def test_missing_profile_is_a_harness_error(tmp_path: Path) -> None:
    """A profile path that does not exist is exit 3, not a refusal."""
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    out_dir = tmp_path / "out"

    result = _build(
        tmp_path / "no-such-profile.json", out_dir, vault_cmd=f"{sys.executable} {fake}"
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert _zips(out_dir) == []


@NEEDS_WORK_TREE
def test_build_prints_the_assert_command(tmp_path: Path) -> None:
    """The builder states the exact judge command for the artifact it wrote.

    ``build_zips.py`` does not invoke the judge either, so this builder PRINTS
    rather than runs -- one packaging habit instead of two, and this script's
    exit code keeps meaning "the zip was written". That printed line is the
    only thing telling the operator to pass ``--custom`` and which tier, so a
    silent regression in it is a handoff shipped unjudged.
    """
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-assert",
            "zip_tier": "enhanced",
            "config": HAPPY_CONFIG,
            "secrets": HAPPY_SECRETS,
        },
    )
    out_dir = tmp_path / "out"

    result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {fake}")

    assert result.returncode == 0, result.stdout + result.stderr

    printed = [line for line in result.stdout.splitlines() if line.startswith("ASSERT WITH:")]
    assert len(printed) == 1, result.stdout
    assert "scripts/packaging/assert_zip.py --custom " in printed[0]
    assert str(_zips(out_dir)[0]) in printed[0]
    assert printed[0].endswith(" enhanced")
    assert FAKE_PREFIX not in result.stdout, "the builder must never print a resolved secret"


@NEEDS_WORK_TREE
def test_install_id_defaults_to_the_profile_name(tmp_path: Path) -> None:
    """A profile that never mentions ``install_id`` still ships an isolated zip.

    Isolation has to be the DEFAULT, not an option a packager remembers. Two
    managed installs on one machine otherwise share a config directory, a data
    directory, and a keychain account, and the second ``auth`` silently
    overwrites the first mailbox's token -- which surfaces as mail filed into
    the wrong client's folders, not as a packaging error anybody would trace
    back to here.
    """
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-test",
            "zip_tier": "enhanced",
            "config": HAPPY_CONFIG,
            "secrets": HAPPY_SECRETS,
        },
    )
    out_dir = tmp_path / "out"

    result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {fake}")

    assert result.returncode == 0, result.stdout + result.stderr
    _, merged, _ = _read_artifact(_zips(out_dir)[0])
    assert merged["install_id"] == "acme-test"


@NEEDS_WORK_TREE
def test_explicit_install_id_in_the_profile_wins(tmp_path: Path) -> None:
    """The default fills a gap; it never overrides a stated answer.

    A packager who needs two zips to share one namespace -- a rebuild for the
    same client under a new profile name, say -- says so in the overlay, and
    the merge treats it like any other override.
    """
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-test",
            "zip_tier": "enhanced",
            "config": {**HAPPY_CONFIG, "install_id": "acme-prod"},
            "secrets": HAPPY_SECRETS,
        },
    )
    out_dir = tmp_path / "out"

    result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {fake}")

    assert result.returncode == 0, result.stdout + result.stderr
    _, merged, _ = _read_artifact(_zips(out_dir)[0])
    assert merged["install_id"] == "acme-prod"


@NEEDS_WORK_TREE
def test_an_untracked_matching_file_does_not_break_the_build(tmp_path: Path) -> None:
    """A stray file under a shipped folder is skipped, not fatal.

    The builder reads member bytes with ``git show HEAD:./<path>``, so anything
    on disk that git cannot serve from ``HEAD`` used to kill the run with a raw
    exit 128. The member list is tracked-filtered and now HEAD-filtered too, so
    a leftover file another lane dropped into ``docs/`` is simply not a member.
    """
    stray = CONNECTOR_ROOT / "docs" / "zz-untracked-stray-for-test.md"
    stray.write_text("not committed\n", encoding="utf-8")
    fake = _write_vault(tmp_path, FAKE_VAULT, "vault_ok.py")
    profile = _write_profile(
        tmp_path,
        {
            "name": "acme-test",
            "zip_tier": "enhanced",
            "config": HAPPY_CONFIG,
            "secrets": HAPPY_SECRETS,
        },
    )
    out_dir = tmp_path / "out"

    try:
        result = _build(profile, out_dir, vault_cmd=f"{sys.executable} {fake}")
    finally:
        stray.unlink()

    assert result.returncode == 0, result.stdout + result.stderr
    names, _, _ = _read_artifact(_zips(out_dir)[0])
    assert "docs/zz-untracked-stray-for-test.md" not in names


def test_uncommitted_members_are_named_not_a_raw_git_128() -> None:
    """A member git cannot serve from ``HEAD`` is refused BY NAME.

    A staged-but-never-committed file is tracked, so it survives the tracked
    filter and reaches ``git show HEAD:./<path>``, which exits 128 and names
    nothing useful. The refusal now says which paths to commit.
    """
    module = _load_builder_module()
    with pytest.raises(module.BuildError) as caught:
        module._committed_members(["README.md", "docs/never-committed-xyz.md"])
    assert "docs/never-committed-xyz.md" in str(caught.value)
    assert "README.md" not in str(caught.value)
