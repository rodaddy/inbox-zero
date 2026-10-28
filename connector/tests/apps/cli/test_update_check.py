"""``update-check`` prints one verdict and never replaces anything (#67, D13).

The HTTP call is stubbed rather than made: the thing under test is the verdict
this install draws from an answer, not kit-links' ability to give one, and a
test that reached the real host would go red the day someone promoted a build.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from inbox_zero_connector.apps.cli import update


@pytest.fixture
def install_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake unzipped kit root carrying the two files the packaging step writes."""
    (tmp_path / "VERSION").write_text("0.2.14\n", encoding="utf-8")
    (tmp_path / "TIER").write_text("basic\n", encoding="utf-8")
    monkeypatch.setattr(update, "_ROOT", tmp_path)
    return tmp_path


def _stub_response(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    """Make ``httpx.get`` answer ``payload`` with a 200, for any URL."""

    def fake_get(url: str, **_: object) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(update.httpx, "get", fake_get)


def test_same_version_reports_up_to_date(
    install_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_response(monkeypatch, {"stable": "0.2.14"})
    assert update.cmd_update_check() == 0
    assert capsys.readouterr().out.strip() == "up to date (0.2.14)"


def test_newer_stable_reports_the_update_and_how_to_take_it(
    install_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_response(monkeypatch, {"stable": "0.3.0"})
    # Exit 1, not 0: a newer build is an ACTIONABLE state a scheduler can see.
    assert update.cmd_update_check() == 1
    out = capsys.readouterr().out
    assert "update available: 0.2.14 -> 0.3.0" in out
    # The path to the zip, and the promise about what survives, are the reason
    # the message exists -- "an update exists" alone is a dead end.
    assert update.KIT_PAGE_URL in out
    assert "config, keychain entries and ledger are kept" in out


def test_an_unreachable_host_is_reported_not_fatal(
    install_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(url: str, **_: object) -> httpx.Response:
        raise httpx.ConnectError("no route", request=httpx.Request("GET", url))

    monkeypatch.setattr(update.httpx, "get", boom)
    assert update.cmd_update_check() == 3
    assert capsys.readouterr().out.startswith("could not check (")
