"""Tests for the ``digest`` and ``logout`` CLI verbs.

Both are about what happens on the UNHAPPY path, which is the only reason
either needed changing:

* ``digest <bad-date>`` used to let ``date.fromisoformat``'s ValueError escape
  ``main``, so a typo produced a Python traceback. A traceback is not an
  operator error message -- it reads as a crash in the audit surface, which is
  the one surface that must never look broken when the input was simply wrong.
* ``logout`` deleted a keychain credential with no confirmation at all. The
  next command after it is an interactive browser consent, so the cost is real
  and the operator gets no chance to notice they named the wrong provider.

``digest --as-draft`` (#65, Rico decision D10/D20) is the one HAPPY-path arm
here, and it is tested for the same reason: its failure mode is silent success.
A run that prints the digest and creates no draft looks identical to one that
delivered, so the assertions are on the provider CALL -- subject, body, and the
posture refusal that must produce no call at all.

Driven through ``cli.main`` so the parser row, the dispatch entry, and the exit
code are proven together. No network, no keychain write -- ``delete_token`` is
monkeypatched, and the provider is a stub carrying only ``create_self_draft``.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.cli import main as cli


@pytest.fixture
def deletions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``delete_token`` call instead of touching the OS keychain."""
    calls: list[str] = []

    def _delete(provider: str) -> bool:
        calls.append(provider)
        return True

    monkeypatch.setattr("inbox_zero_connector.services.auth.keychain.delete_token", _delete)
    return calls


# --------------------------------------------------------------------------
# digest


def test_digest_rejects_a_malformed_date_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 1 with a message naming the input and the expected shape."""
    assert cli.main(["digest", "not-a-date"]) == 1
    err = capsys.readouterr().err
    assert "not-a-date" in err
    assert "YYYY-MM-DD" in err
    assert "Traceback" not in err


def test_digest_rejects_an_impossible_date(capsys: pytest.CaptureFixture[str]) -> None:
    """Well-shaped but not a real day is the same operator error, same exit."""
    assert cli.main(["digest", "2026-02-30"]) == 1
    assert "2026-02-30" in capsys.readouterr().err


def test_digest_accepts_a_valid_date(capsys: pytest.CaptureFixture[str]) -> None:
    """The happy path is unchanged: exit 0, digest on stdout."""
    assert cli.main(["digest", "2026-08-23"]) == 0
    assert capsys.readouterr().out.strip()


def test_digest_with_no_argument_still_defaults_to_today(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``None`` is not a parse failure -- it is the ledger's own default day."""
    assert cli.main(["digest"]) == 0
    assert capsys.readouterr().out.strip()


class _DraftSpy:
    """A provider stub whose ONLY reachable capability is the digest draft."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_self_draft(self, subject: str, body: str) -> str:
        self.calls.append((subject, body))
        return "DRAFT_1"


def _wire(monkeypatch: pytest.MonkeyPatch, *, drafts_allowed: bool) -> _DraftSpy:
    """Point ``digest`` at a stub provider and a chosen drafts posture."""
    from inbox_zero_connector.apps.cli import digest as digest_cli

    spy = _DraftSpy()
    monkeypatch.setattr(
        digest_cli,
        "load_config",
        lambda: type("C", (), {"drafts_allowed": lambda _s, _p: drafts_allowed})(),
    )
    monkeypatch.setattr(
        digest_cli, "load_settings", lambda: type("S", (), {"provider": "google"})()
    )
    monkeypatch.setattr(digest_cli, "get_provider", lambda *a, **k: spy)
    return spy


def test_as_draft_leaves_the_digest_in_the_mailbox_with_the_dated_subject(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The subject is fixed and dated, and the body is the digest as printed (#65)."""
    spy = _wire(monkeypatch, drafts_allowed=True)

    assert cli.main(["digest", "2026-08-23", "--as-draft"]) == 0

    assert len(spy.calls) == 1
    subject, body = spy.calls[0]
    assert subject == "Inbox Zero digest 2026-08-23"
    assert body == capsys.readouterr().out.split("\nLeft the digest")[0].rstrip("\n")


def test_as_draft_refuses_when_the_drafts_posture_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """POSTURE WINS: one plain sentence naming the flag, exit 1, no draft.

    Falling back to stdout-only would report success at the thing the operator
    did not ask for, so the refusal is the whole outcome.
    """
    spy = _wire(monkeypatch, drafts_allowed=False)

    assert cli.main(["digest", "2026-08-23", "--as-draft"]) == 1

    assert spy.calls == []
    err = capsys.readouterr().err
    assert "--as-draft" in err
    assert "drafts_enabled.google" in err


def test_digest_without_as_draft_creates_no_draft(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag is opt-in, and it must not leak across invocations."""
    spy = _wire(monkeypatch, drafts_allowed=True)

    assert cli.main(["digest", "2026-08-23"]) == 0

    assert spy.calls == []
    capsys.readouterr()


# --------------------------------------------------------------------------
# logout


def test_logout_without_yes_refuses_when_stdin_is_not_a_terminal(
    deletions: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scripted run cannot be prompted, so it is refused rather than guessed at."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert cli.main(["logout", "google"]) == 1
    assert deletions == []
    assert "--yes" in capsys.readouterr().err


def test_logout_with_yes_deletes(deletions: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """``--yes`` is the non-interactive consent and needs no terminal."""
    assert cli.main(["logout", "--yes", "google"]) == 0
    assert deletions == ["google"]
    assert "Removed" in capsys.readouterr().out


def test_interactive_confirmation_names_the_provider_and_install_id(
    deletions: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt states WHICH credential on WHICH install, then deletes on yes."""
    from inbox_zero_connector.config import install_id

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    assert cli.main(["logout", "microsoft"]) == 0
    assert deletions == ["microsoft"]
    out = capsys.readouterr().out
    assert "microsoft" in out
    assert install_id() in out


def test_interactive_refusal_deletes_nothing(
    deletions: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything but an explicit yes is a no, and the exit says nothing happened."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert cli.main(["logout", "microsoft"]) == 1
    assert deletions == []
    assert "Nothing was removed" in capsys.readouterr().out
