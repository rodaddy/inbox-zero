"""Tests for the ``diagnose`` CLI subcommand -- the beta diagnostics bundle.

The bundle is what a beta tester sends BACK, so the tests that matter are the
negative ones: a planted API key written into ``secrets.json`` and quoted by a
log line must not survive into any member, and ``secrets.json`` itself must not
be a member at all. The positive tests pin the member list and the manifest
that describes it, so a member added later cannot go unlisted.

No mail, no model, no network, no credential. The autouse ``isolated_dirs``
fixture in ``conftest.py`` sandboxes config and data under ``tmp_path``, and
the command is driven through ``cli.main`` like ``test_show_scopes.py`` so the
parser row, the dispatch entry, and the exit code are proven together.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from inbox_zero_connector.apps.cli import diagnose
from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.config import config_path, data_dir, load_settings, secrets_path
from inbox_zero_connector.services.audit import ledger, trace
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy, write_taxonomy

# Assembled at import time so the canary is never a literal a secret scanner
# (gitleaks, pre-commit) has to be taught to ignore.
PLANTED_KEY = "-".join(("sk", "PLANTED", "9f8e7d6c5b4a"))
# Not a credential -- a canary string this suite plants and then proves absent.
PLANTED_SECRET = "GOCSPX-PLANTED-xyz"  # ruff: ignore[hardcoded-password-string]


def _seed_config() -> None:
    """Write an installed config the same way the CLI's own installer does."""
    assert cli.main(["init-config", "--owner-domain", "example.com"]) == 0


def _log_dir() -> Path:
    """The directory the diagnose bundle tails, resolved the way it resolves it."""
    directory = Path(load_settings(configure_logging=False).logging.directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _only_zip(out: Path) -> Path:
    zips = sorted(out.glob("*.zip"))
    assert len(zips) == 1, f"expected exactly one zip, found {zips}"
    return zips[0]


def _run(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    assert cli.main(["diagnose", "--out", str(out)]) == 0
    return _only_zip(out)


def test_diagnose_writes_a_zip_with_the_expected_members(tmp_path: Path) -> None:
    """Every installed artifact present on disk becomes exactly one member."""
    _seed_config()
    ledger.append("microsoft", "file", "msg-1", "Reference")
    trace.append(
        trace.Subject(
            provider="microsoft",
            message_id="msg-1",
            thread_id="thr-1",
            from_domain="example.com",
            folder_before="Inbox",
        ),
        Classification(folder="Reference", confidence=0.9, reason="rule", deterministic=True),
    )
    (data_dir() / "watchstate-microsoft.json").write_text("{}", encoding="utf-8")
    write_taxonomy(default_taxonomy())

    archive = _run(tmp_path)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        digest = zf.read("digest-7d.txt").decode("utf-8")

    for expected in (
        "decision-trace.jsonl",
        "ledger.jsonl",
        "watchstate-microsoft.json",
        "connector-config.json",
        "taxonomy.json",
        "show-scopes.txt",
        "digest-7d.txt",
        "versions.txt",
        "manifest.json",
    ):
        assert expected in names, f"{expected} missing from {names}"

    listed = [member["name"] for member in manifest["members"]]
    assert listed == [name for name in names if name != "manifest.json"]
    assert digest.count("## ") == 7


def test_secrets_file_and_a_planted_secret_never_land_in_the_zip(tmp_path: Path) -> None:
    """A key in secrets.json, and the same key quoted by a log, are both scrubbed."""
    _seed_config()
    secrets_path().write_text(
        json.dumps({
            "credentials": {
                "classifier_api_key": PLANTED_KEY,
                "google_client_secret": PLANTED_SECRET,
            }
        }),
        encoding="utf-8",
    )
    (_log_dir() / "inbox-zero-connector-test.log").write_text(
        f"INFO would never do this but: key={PLANTED_KEY}\n", encoding="utf-8"
    )

    archive = _run(tmp_path)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert not [name for name in names if name.endswith("secrets.json")]
        for name in names:
            body = zf.read(name)
            assert PLANTED_KEY.encode() not in body, f"planted key survived in {name}"
            assert PLANTED_SECRET.encode() not in body, f"planted secret survived in {name}"
        planted = "logs/inbox-zero-connector-test.log"
        assert planted in names, f"no planted log member in {names}"
        assert "[REDACTED]" in zf.read(planted).decode("utf-8")


def test_diagnose_with_nothing_installed_still_exits_zero(tmp_path: Path) -> None:
    """A bundle from a broken install is exactly when one is most needed."""
    archive = _run(tmp_path)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    for expected in ("manifest.json", "versions.txt", "digest-7d.txt", "show-scopes.txt"):
        assert expected in names, f"{expected} missing from {names}"


def test_log_tail_is_capped_at_500_lines(tmp_path: Path) -> None:
    """700 lines in, the LAST 500 out -- a bundle is a tail, not an archive."""
    _seed_config()
    lines = [f"line {index}" for index in range(700)]
    (_log_dir() / "big.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    archive = _run(tmp_path)
    with zipfile.ZipFile(archive) as zf:
        body = zf.read("logs/big.log").decode("utf-8")
    kept = body.splitlines()
    assert len(kept) == 500
    assert kept == lines[-500:]


def test_diagnose_never_prints_a_secret_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command's own stdout is a surface too, and it is read aloud in support."""
    _seed_config()
    secrets_path().write_text(
        json.dumps({"credentials": {"classifier_api_key": PLANTED_KEY}}), encoding="utf-8"
    )
    _run(tmp_path)
    out = capsys.readouterr().out
    assert PLANTED_KEY not in out


def test_install_transcripts_ship_whole_and_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install transcript is the install RECORD, so it ships whole, not tailed."""
    monkeypatch.setattr(diagnose, "PROJECT_ROOT", tmp_path / "root")
    install_logs = tmp_path / "root" / "logs"
    install_logs.mkdir(parents=True)
    lines = [f"install line {index}" for index in range(700)]
    lines[42] = f"INFO would never do this but: key={PLANTED_KEY}"
    (install_logs / "install-20260822T000000Z.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    _seed_config()
    secrets_path().write_text(
        json.dumps({"credentials": {"classifier_api_key": PLANTED_KEY}}), encoding="utf-8"
    )

    archive = _run(tmp_path)
    member = "logs/install-20260822T000000Z.log"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert member in names, f"{member} missing from {names}"
        assert names.count(member) == 1, f"duplicate {member} in {names}"
        body = zf.read(member).decode("utf-8")

    assert len(body.splitlines()) == 700, "install transcript was tailed, not shipped whole"
    assert PLANTED_KEY not in body
    assert "[REDACTED]" in body


def test_jsonl_log_is_bundled_and_scrubbed(tmp_path: Path) -> None:
    """The structured sink is a log file too, and it is the one support parses.

    ``logging_config`` writes ``<service>-<env>.log`` and ``<service>-<env>.jsonl``
    side by side. A glob of ``*.log`` alone shipped the human-readable half and
    silently dropped the machine-readable half -- the half a support engineer
    actually greps -- so the bundle answered "what happened" with the weaker of
    the two copies it had.
    """
    _seed_config()
    secrets_path().write_text(
        json.dumps({"credentials": {"classifier_api_key": PLANTED_KEY}}), encoding="utf-8"
    )
    (_log_dir() / "inbox-zero-connector-test.jsonl").write_text(
        json.dumps({"text": f"key={PLANTED_KEY}"}) + "\n", encoding="utf-8"
    )

    archive = _run(tmp_path)
    member = "logs/inbox-zero-connector-test.jsonl"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert member in names, f"{member} missing from {names}"
        body = zf.read(member).decode("utf-8")
    assert PLANTED_KEY not in body
    assert "[REDACTED]" in body


def test_config_held_credential_value_is_scrubbed_from_every_member(tmp_path: Path) -> None:
    """The scrub vocabulary reads ``connector-config.json`` too, not just secrets.json.

    #148: a ``google_client_secret`` hand-typed into the config file was bundled
    verbatim, because the scrub source was ``secrets.json`` alone -- the one file
    that is structurally never a member. The value that COULD ship was the only
    one the redaction pass could not see.
    """
    _seed_config()
    config = json.loads(config_path().read_text(encoding="utf-8"))
    config["credentials"] = {"google_client_secret": PLANTED_SECRET}
    config_path().write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (_log_dir() / "inbox-zero-connector-test.log").write_text(
        f"INFO would never do this but: secret={PLANTED_SECRET}\n", encoding="utf-8"
    )

    archive = _run(tmp_path)
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            assert PLANTED_SECRET.encode() not in zf.read(name), f"secret survived in {name}"


def test_bundle_claim_names_what_the_scrub_actually_covers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed promise and the manifest's ``never_included`` must both be true.

    The old stdout line claimed "no secrets.json" -- true but narrower than the
    thing the operator needs to hear, since the bundle DOES carry
    ``connector-config.json``. The claim now names the scrub, which the two tests
    above prove.
    """
    _seed_config()
    archive = _run(tmp_path)
    out = capsys.readouterr().out
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    assert "secrets.json" in out
    assert "connector-config.json" in out, "the claim must admit what the bundle carries"
    never = " ".join(manifest["never_included"])
    assert "secrets.json" in never
    assert "credential value" in never.lower()
