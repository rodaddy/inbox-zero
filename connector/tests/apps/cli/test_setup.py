"""Tests for the ``setup`` CLI subcommand -- the interactive first-install wizard.

No mail, no model, no network, no credential. Every answer arrives through a
monkeypatched ``builtins.input``, and the autouse ``isolated_dirs`` fixture in
``conftest.py`` sandboxes the config and secrets files under ``tmp_path``, so a
run here can never reach the operator's real installed config.

Three properties are what these tests actually pin down, and each one is a
posture claim the wizard makes rather than a convenience:

* **It refuses without a TTY.** ``setup`` is an install step performed by a
  human at a terminal. A host AI reaching it through an MCP tool call would be
  the model configuring its own permissions -- the same posture ``auth`` takes
  -- so the gate is checked FIRST and nothing is written when it trips.
* **It never says the word "secret" in a prompt.** The wizard asks for OAuth
  CLIENT IDENTIFIERS, which are not secrets and belong in the file the installer
  rewrites. The VALUES (``google_client_secret``, ``classifier_api_key``) are
  never prompted for at all: the wizard creates the empty skeleton and points
  the human at it. ``test_setup_never_utters_the_word_secret_in_a_prompt``
  records every prompt string the run produced and asserts the whole set is
  clean, so a future prompt worded "client secret" fails here rather than
  teaching an installer to paste a credential into a terminal.
* **The answers reach disk unchanged.** The written JSON is parsed rather than
  eyeballed, so the posture, the per-provider drafts flags, the routing, the
  vendor switch, and exactly the three identifiers are proven together.

Exercised through ``cli.main`` like ``test_show_scopes.py`` and
``test_desktop_config.py``, so the parser row, the dispatch row, and the exit
code are proven with the behavior.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.config import config_path, secrets_path

# The full happy-path script, in prompt order: the ACCOUNT LIST first and whole
# (#55) -- every question answerable from memory, before anything opens a
# consent window -- then the shared settings, then the three OAuth identifiers.
#
# TWO accounts, differing on posture AND drafts, because that split is the
# decision under test (D11): taxonomy stays shared, posture and drafts are per
# account. A one-account script could not tell the two apart.
FULL_RUN: tuple[str, ...] = (
    # Account 1: a consumer Microsoft domain, so the provider is INFERRED and
    # the wizard does not ask for it.
    "owner@outlook.com",
    "example.com",  # owner domain, overriding the default from the address
    "file_read_draft",  # posture, per account
    "y",  # drafts on this account
    "y",  # add another mailbox?
    # Account 2: a custom domain states no provider, so the wizard ASKS.
    "second@example.net",
    "google",  # provider, asked
    "",  # owner domain: accept the default taken from the address
    "",  # posture: accept the file_only default
    "n",  # drafts off on this account
    "n",  # add another mailbox?
    "sales,leads",
    "http://localhost:7101/v1",
    "",  # model: accept the ClassifierSettings default
    "n",  # requires_key
    "abc.apps.googleusercontent.com",
    "app-123",
    "",  # tenant: accept the "common" default
)


class _Answers:
    """A scripted ``input()`` that ALSO records the prompt it was asked.

    The recording is not incidental. The wizard's prompts are themselves under
    test -- one of them must never contain the word "secret" -- and a plain
    ``iter(...).__next__`` throws the prompt away, leaving that property
    unprovable. Keeping the strings makes it an assertion instead of a habit.

    An answer running out raises ``AssertionError`` rather than
    ``StopIteration``, because a wizard that asks one more question than the
    script expects is a test failure with a name, not an obscure traceback out
    of the iterator protocol.
    """

    def __init__(self, answers: tuple[str, ...]) -> None:
        self.prompts: list[str] = []
        self._answers: Iterator[str] = iter(answers)

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        try:
            return next(self._answers)
        except StopIteration:  # pragma: no cover - only on a script mismatch
            raise AssertionError(f"wizard asked an unscripted question: {prompt!r}") from None


class _FakeStream:
    """Minimal stand-in for ``sys.stdin``/``sys.stdout`` with a chosen isatty."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:  # pragma: no cover - stdout path only
        return len(text)

    def flush(self) -> None:  # pragma: no cover - stdout path only
        return None


def _tty(monkeypatch: pytest.MonkeyPatch, *, value: bool) -> None:
    """Force both standard streams to report the given ``isatty``.

    Patched on the real ``sys.stdin``/``sys.stdout`` objects that pytest's
    capture has already installed, so stdout keeps being CAPTURED -- swapping
    stdout for a fake object would make ``capsys`` see nothing.
    """
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: value, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value, raising=False)


def _script(monkeypatch: pytest.MonkeyPatch, answers: tuple[str, ...]) -> _Answers:
    """Install a scripted ``input()`` and hand back its recorder."""
    scripted = _Answers(answers)
    monkeypatch.setattr("builtins.input", scripted)
    return scripted


def _written() -> dict[str, Any]:
    """Parse the installed config the wizard just wrote."""
    return json.loads(config_path().read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_setup_refuses_without_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No terminal, no wizard -- and nothing on disk to show for the attempt."""
    _tty(monkeypatch, value=False)
    monkeypatch.setattr("builtins.input", _Answers(FULL_RUN))

    assert cli.main(["setup"]) == 1

    err = capsys.readouterr().err.lower()
    assert "interactive" in err
    assert "terminal" in err
    assert not config_path().exists()


def test_setup_writes_the_config_from_answers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A full scripted run lands every answer in the installed file."""
    _tty(monkeypatch, value=True)
    _script(monkeypatch, FULL_RUN)

    assert cli.main(["setup"]) == 0
    capsys.readouterr()

    written = _written()

    # The FIRST account is the install's default, so these install-level fields
    # describe it -- which is what keeps every existing single-account caller
    # working against a config that now also carries a list.
    assert written["posture"] == "file_read_draft"
    # Per PROVIDER, folded from the per-account flags: Microsoft is on because
    # account 1 asked for drafts, Google is off because account 2 did not.
    assert written["drafts_enabled"] == {"google": False, "microsoft": True}

    routing = written["voice_routing"]
    assert routing["owner_domain"] == "example.com"
    assert list(routing["sales_labels"]) == ["sales", "leads"]

    # The whole list reached disk, in order, with the per-account split intact.
    accounts = written["accounts"]
    assert [entry["address"] for entry in accounts] == [
        "owner@outlook.com",
        "second@example.net",
    ]
    # Provider INFERRED for the consumer domain, ASKED for the custom one.
    assert [entry["provider"] for entry in accounts] == ["microsoft", "google"]
    # D11: posture and drafts differ per account; nothing collapsed them.
    assert [entry["posture"] for entry in accounts] == ["file_read_draft", "file_only"]
    assert [entry["drafts"] for entry in accounts] == [True, False]
    # The second account's owner domain came from its own address by default.
    assert [entry["owner_domain"] for entry in accounts] == ["example.com", "example.net"]

    classifier = written["classifier"]
    assert classifier["base_url"] == "http://localhost:7101/v1"
    assert classifier["requires_key"] is False
    assert classifier["model"]  # the accepted default, whatever it is today

    # EXACTLY the three identifiers, and no credential VALUE field beside them.
    assert written["credentials"] == {
        "google_client_id": "abc.apps.googleusercontent.com",
        "microsoft_client_id": "app-123",
        "microsoft_tenant_id": "common",
    }

    # The skeleton exists for the human to fill in themselves.
    assert secrets_path().is_file()


def test_setup_never_utters_the_word_secret_in_a_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prompts ask for identifiers only; the secrets FILE is named in output."""
    _tty(monkeypatch, value=True)
    scripted = _script(monkeypatch, FULL_RUN)

    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out

    assert scripted.prompts, "the wizard asked nothing at all"
    offenders = [p for p in scripted.prompts if "secret" in p.lower()]
    assert not offenders, f"prompt asked for a secret: {offenders!r}"

    # Not prompted for, but pointed AT -- the human opens this file themselves.
    assert str(secrets_path()) in out


def test_setup_declines_to_overwrite(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing config plus anything but yes leaves the file byte-identical."""
    target: Path = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"posture": "file_only", "drafts_enabled": {}}'
    target.write_bytes(original)

    _tty(monkeypatch, value=True)
    _script(monkeypatch, ("n",))

    assert cli.main(["setup"]) == 1
    capsys.readouterr()
    assert target.read_bytes() == original
