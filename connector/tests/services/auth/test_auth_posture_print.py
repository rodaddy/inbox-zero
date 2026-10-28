"""Every drafts line one ``auth microsoft`` run emits must agree (#49).

The ``auth`` consent screen is the moment the buyer decides, and it is pitched
as the record of what was consented to. Two independent readings of the same
config reach that operator in one run: ``cmd_auth`` PRINTS the drafts flag and
the computed enforcement layer to stdout, while ``scopes.enforcement_for`` and
``scopes.scopes_for`` LOG their own reading at DEBUG. An operator running with
debug logging on sees both streams interleaved, so a disagreement between them
is a contradiction in one screen, not in two places.

This asserts the invariant rather than a fixed string: whatever the installed
config says, every line mentioning drafts -- printed or logged -- reports that
same value. The provider is exercised through ``cli.main`` like
``tests/apps/cli/test_show_scopes.py``, so the parser row and dispatch are in
the path; the interactive consent itself is stubbed, because the printing under
test happens BEFORE the browser opens and no test may touch a real keychain.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from loguru import logger

from inbox_zero_connector.apps.cli import main as cli

# Both the printed summary (``Drafts enabled for microsoft: True``) and the
# DEBUG records (``... drafts_enabled=True ...``) are matched by one pattern,
# so the assertion cannot be satisfied by a stream it forgot to look at.
_DRAFTS_VALUE = re.compile(
    r"drafts(?:_enabled)?(?:\s+enabled\s+for\s+\w+)?\s*[:=]\s*(True|False)", re.IGNORECASE
)


@pytest.fixture
def captured_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Run ``auth`` with consent stubbed, collecting every DEBUG record.

    ``logging_config.setup`` calls ``logger.remove()`` to rebuild its sinks, so
    a sink added before the command runs would be torn down mid-run and this
    test would assert over an EMPTY log stream while still passing. Neutering
    ``remove`` for the duration is what keeps the DEBUG arm honest;
    ``_captured_scopes_lines`` then asserts the records really arrived.
    """
    records: list[str] = []

    class _StubProvider:
        def authenticate(self, interactive: bool = True) -> str:
            return "stub@example.com"

    monkeypatch.setattr(
        "inbox_zero_connector.apps.cli.credentials.get_provider",
        lambda name, drafts_enabled=False: _StubProvider(),
    )
    monkeypatch.setattr(logger, "remove", lambda *a, **k: None)
    logger.add(records.append, level="DEBUG", format="{message}")
    yield records


def _captured_scopes_lines(records: list[str]) -> None:
    """Fail loudly if the DEBUG arm captured none of the lines it exists for."""
    assert any("SCOPES:" in r for r in records), (
        "no SCOPES DEBUG record was captured; the log arm of this test is vacuous"
    )


def _drafts_lines(stdout: str, records: list[str]) -> list[str]:
    lines = [ln for ln in stdout.splitlines() + records if _DRAFTS_VALUE.search(ln)]
    assert lines, "no line mentioned drafts at all; the harness is not exercising the path"
    return lines


def _assert_all_agree(lines: list[str], expected: bool) -> None:
    disagreeing = [
        ln
        for ln in lines
        if (m := _DRAFTS_VALUE.search(ln)) and m.group(1).lower() != str(expected).lower()
    ]
    assert not disagreeing, f"configured drafts={expected} but these lines disagree:\n" + "\n".join(
        disagreeing
    )


def test_microsoft_drafts_on_every_line_agrees(
    captured_run: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """With Microsoft drafts ON, no line may report ``False``."""
    assert (
        cli.main([
            "init-config",
            "--owner-domain",
            "example.com",
            "--posture",
            "file_read_draft",
            "--enable-microsoft-drafts",
        ])
        == 0
    )
    capsys.readouterr()
    captured_run.clear()

    assert cli.main(["auth", "microsoft"]) == 0
    out = capsys.readouterr().out
    _captured_scopes_lines(captured_run)
    _assert_all_agree(_drafts_lines(out, captured_run), True)


def test_microsoft_drafts_off_every_line_agrees(
    captured_run: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Default config is drafts-off; no line may report ``True``."""
    assert cli.main(["auth", "microsoft"]) == 0
    out = capsys.readouterr().out
    _captured_scopes_lines(captured_run)
    _assert_all_agree(_drafts_lines(out, captured_run), False)
