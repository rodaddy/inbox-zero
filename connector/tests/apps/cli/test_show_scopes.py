"""Tests for the ``show-scopes`` CLI subcommand's annotations (#106).

No mail, no model, no network, no credential — the command renders the scope
manifest computed from the installed config, and the autouse ``isolated_dirs``
fixture in ``conftest.py`` sandboxes that config under ``tmp_path``.

The defect these tests pin down: ``gmail.modify`` is the interactive flow's
one scope, so it is requested with drafts OFF too, and it is send-capable —
but the annotation on that line was hard-coded ``(drafts are on)``, printing a
self-contradiction two lines under ``drafts=False``. This command is pitched
as security-questionnaire evidence, so the annotation must track the actual
drafts flag.

Exercised through ``cli.main`` like ``test_cli_rules.py``, so the parser row,
dispatch, and exit code are proven together.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.cli import main as cli


def test_drafts_off_never_claims_drafts_are_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default config is drafts-off everywhere; no line may say drafts are on."""
    assert cli.main(["show-scopes"]) == 0
    out = capsys.readouterr().out
    assert "drafts=False" in out
    assert "drafts are on" not in out
    # The scope itself is still requested and still honestly flagged.
    assert "REQUESTED and send-capable: https://www.googleapis.com/auth/gmail.modify" in out


def test_drafts_on_annotation_survives(capsys: pytest.CaptureFixture[str]) -> None:
    """With Gmail drafts enabled, the same line DOES say drafts are on."""
    assert (
        cli.main([
            "init-config",
            "--owner-domain",
            "example.com",
            "--posture",
            "file_read_draft",
            "--enable-google-drafts",
        ])
        == 0
    )
    capsys.readouterr()  # drop init-config output
    assert cli.main(["show-scopes"]) == 0
    out = capsys.readouterr().out
    assert "google: drafts=True" in out
    assert "(drafts are on)" in out


def test_closing_paragraph_matches_header_layer_for_default_google(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The closing prose must report the SAME layer the header computed.

    Default config is drafts-off + interactive auth, whose honest posture is
    CODE-LAYER (``gmail.modify`` is send-capable). The closing paragraph used
    to branch on ``drafts_enabled`` instead of the computed enforcement, so it
    announced SCOPE-LAYER directly under a header line reading
    ``no-send = code-layer`` -- a self-contradiction in the one output pitched
    as security-questionnaire evidence.
    """
    assert cli.main(["show-scopes"]) == 0
    out = capsys.readouterr().out
    assert "google: drafts=False, no-send = code-layer" in out
    gmail_prose = out[out.index("Gmail drafts are") : out.index("Microsoft is")]
    assert "Gmail drafts are OFF" in gmail_prose
    assert "CODE-LAYER" in gmail_prose
    assert "SCOPE-LAYER" not in gmail_prose
    # Microsoft's posture really IS scope-layer, and its own paragraph says so.
    assert "microsoft: drafts=False, no-send = scope-layer" in out
    assert "Microsoft is SCOPE-LAYER" in out
