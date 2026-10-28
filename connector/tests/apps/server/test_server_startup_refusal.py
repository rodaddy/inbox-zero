"""``serve`` startup refuses a half-finished install in one line, not a stack (#193).

``serve`` is the DEFAULT command -- ``main()`` resolves ``args.command or
"serve"`` -- and it is what the shipped Claude Desktop ``mcpServers`` entry
launches (``apps/cli/desktop.py``). It is therefore the FIRST thing a fresh
unzip runs, and the ordinary state of an install that has not finished setup is
a missing taxonomy. That is a sentence the installer can act on ("Run 'uv run
inbox-zero-connector init-taxonomy'"), not a defect anybody should read a
traceback for.

The startup arm used to key on the CONCRETE exception class, and the two arms
in the same function disagreed as a result: ``ConfigError`` raised ``SystemExit``
with one clean line, while its sibling ``TaxonomyError`` first went through
``logger.opt(exception=True)`` and put roughly 215 lines of stack on stderr
ahead of the same one-line exit. Both subclass ``OperatorRefusalError``,
which exists precisely so a refusal cannot be treated as a crash because of
which subclass it happens to be. The arms now catch the BASE.

The tests below drive the real ``server.main`` -- not a stubbed refusal path --
and assert on real file-descriptor stderr, because that is the stream a desktop
client's log actually collects. The taxonomy case is the residual being closed
and the config case is the neighbour whose behaviour it now matches; asserting
both is what keeps them from drifting apart again, since the original defect WAS
the drift rather than either arm alone. The last test raises a refusal subclass
invented in the test file, which is the only assertion here that can tell
"catches the base" apart from "names both concrete classes" -- and the latter
would reopen the defect on whatever subclass is added next.
"""

from __future__ import annotations

import json

import pytest

from inbox_zero_connector.apps.server import main as server
from inbox_zero_connector.config_paths import config_dir
from inbox_zero_connector.errors import OperatorRefusalError

_REFUSAL = "inbox-zero-connector cannot start:"


def _run_startup(capfd: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Drive ``server.main`` to its startup refusal, returning the exit text and stderr.

    ``mcp.run`` is never reached: both arms raise ``SystemExit`` before it. If a
    future change lets startup through, ``pytest.raises`` fails here rather than
    the suite blocking on a live stdio server, which is the failure mode worth
    being explicit about.
    """
    with pytest.raises(SystemExit) as exit_info:
        server.main()
    captured = capfd.readouterr()
    return str(exit_info.value), captured.err


def test_a_missing_taxonomy_refuses_serve_with_one_line_and_no_traceback(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The #193 residual. A fresh unzip's default command must not dump a stack.

    ``isolated_dirs`` points the config dir at an empty tmp_path and the
    ``installed_taxonomy`` fixture is deliberately NOT requested, so this is a
    genuinely missing taxonomy going through the real ``load_taxonomy`` -- the
    exact state of an install between unzip and ``init-taxonomy``.
    """
    assert not (config_dir() / "taxonomy.json").exists()

    message, err = _run_startup(capfd)

    assert "Traceback" not in err, err
    assert "RedactedException" not in err, err
    assert _REFUSAL in message
    assert "init-taxonomy" in message, message

    refusal_lines = [line for line in err.splitlines() if "taxonomy" in line.lower()]
    assert len(refusal_lines) <= 1, refusal_lines


def test_a_broken_config_refuses_serve_the_same_way(
    capfd: pytest.CaptureFixture[str],
    installed_taxonomy: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The neighbour arm, asserted so the two cannot drift apart again.

    A taxonomy IS installed here, so startup gets past the taxonomy arm and
    fails on the configuration instead -- an unknown provider name, which
    ``load_settings`` refuses with a ``ConfigError``. This arm was already
    clean; pinning it is what makes the pair a contract rather than a
    coincidence.
    """
    monkeypatch.setenv("INBOX_ZERO_PROVIDER", "bogus")

    message, err = _run_startup(capfd)

    assert "Traceback" not in err, err
    assert _REFUSAL in message


def test_a_malformed_taxonomy_file_refuses_serve_without_a_stack(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Malformed, not merely absent -- the arm's other real-world input.

    A taxonomy that parses as JSON but violates the schema raises from a
    different place inside ``load_taxonomy`` than a missing file does, and the
    original defect rendered a LONGER stack for it (13 stderr lines rather than
    9). Same refusal, same one-line contract.
    """
    (config_dir() / "taxonomy.json").write_text(json.dumps({"folders": [123]}), encoding="utf-8")

    message, err = _run_startup(capfd)

    assert "Traceback" not in err, err
    assert _REFUSAL in message


def test_a_refusal_subclass_the_arms_never_named_is_still_caught(
    capfd: pytest.CaptureFixture[str],
    installed_taxonomy: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanism, driven rather than read: the arms catch the BASE.

    The three tests above would all pass on arms that simply named both
    concrete classes and dropped the ``logger.opt`` call -- and those arms would
    silently crash on the NEXT ``OperatorRefusalError`` subclass somebody adds,
    which is the whole defect class the base was introduced to close. So this
    raises a refusal subclass that the startup code does not know exists. If it
    is caught, the arms are keyed on the base; if it escapes ``SystemExit``, they
    are keyed on a list of names.
    """

    class UnnamedRefusalError(OperatorRefusalError):
        """A refusal type invented here, which startup has never heard of."""

    def _refuse() -> None:
        raise UnnamedRefusalError("a refusal subclass main() was never taught")

    monkeypatch.setattr(server, "load_taxonomy", _refuse)

    message, err = _run_startup(capfd)

    assert "Traceback" not in err, err
    assert _REFUSAL in message
    assert "never taught" in message, message
