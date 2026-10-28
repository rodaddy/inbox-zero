"""The ``undo`` verb previews by default and moves mail only on ``--apply`` (#66)."""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.cli import main, undo
from inbox_zero_connector.services.audit import ledger
from inbox_zero_connector.services.audit import undo as undo_service

RUN = "run-aaaa"


def _plan() -> undo_service.UndoPlan:
    """One reversible filing, one folder the run created."""
    return undo_service.UndoPlan(
        steps=(
            undo_service.Reversal(
                message_id="m-1",
                provider="google",
                folder_from="Receipts",
                folder_to="INBOX",
                run_id=RUN,
            ),
        ),
        created=("Receipts",),
        run_id=RUN,
    )


@pytest.fixture
def planned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aim the CLI's two service calls at a fixed plan, so no ledger is needed."""
    monkeypatch.setattr(undo_service, "last_run_id", lambda: RUN)
    monkeypatch.setattr(undo_service, "plan", lambda run_id: _plan())


def test_undo_previews_and_says_it_changed_nothing(
    planned: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """DEFAULT IS STILL DRY. The destructive reading has to be asked for by name."""
    assert undo.cmd_undo() == 0
    out = capsys.readouterr().out
    assert "m-1  Receipts -> INBOX" in out
    assert "Nothing was changed" in out
    assert "--apply" in out


def test_apply_moves_the_mail_back_through_the_provider(
    planned: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--apply``'s whole reason for existing, end to end through the CLI."""
    moves: list[tuple[str, str]] = []

    class FakeProvider:
        name = "google"

        def file_message(self, message_id: str, folder: str) -> str:
            moves.append((message_id, folder))
            return message_id

    # THE REAL ``undo_service.apply`` RUNS. Only the provider is faked, so this
    # exercises the CLI's wiring AND the service's move-and-receipt loop; a
    # stubbed apply would assert only that the CLI called something.
    monkeypatch.setattr(undo, "get_provider", lambda _name: FakeProvider())
    assert undo.cmd_undo(apply_changes=True) == 0
    assert moves == [("m-1", "INBOX")]
    assert "Moved back: 1 of 1" in capsys.readouterr().out


def test_a_partial_restore_exits_non_zero(
    planned: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mailbox left half-restored is a state a scheduler must see in an exit code."""
    monkeypatch.setattr(undo, "get_provider", lambda _name: object())
    monkeypatch.setattr(undo_service, "apply", lambda provider, plan: 0)
    assert undo.cmd_undo(apply_changes=True) == 1
    assert "did not move" in capsys.readouterr().out


def test_an_unreversible_run_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-schema-4 run must not print as "nothing to do" -- it has mail in it."""
    monkeypatch.setattr(undo_service, "last_run_id", lambda: RUN)
    monkeypatch.setattr(
        undo_service,
        "plan",
        lambda run_id: undo_service.UndoPlan(
            steps=(), created=(), run_id=RUN, skipped_unreversible=2
        ),
    )
    assert undo.cmd_undo() == 0
    assert "source folder" in capsys.readouterr().out


def test_no_run_at_all_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty ledger and a failed command look identical on a blank terminal."""
    monkeypatch.setattr(undo_service, "last_run_id", lambda: "")
    monkeypatch.setattr(
        undo_service, "plan", lambda run_id: undo_service.UndoPlan(steps=(), created=())
    )
    assert undo.cmd_undo() == 0
    assert "no watch run has filed anything yet" in capsys.readouterr().out


def test_the_verb_and_its_flags_are_registered() -> None:
    """The dispatch row is reachable, and both selectors and --apply parse."""
    args = main.build_parser().parse_args(["undo", "--run", RUN, "--apply"])
    assert (args.command, args.run, args.apply) == ("undo", RUN, True)
    assert main.build_parser().parse_args(["undo"]).apply is False


def test_last_and_run_cannot_both_be_given() -> None:
    """They are the same question, so a combination would need a precedence rule."""
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["undo", "--last", "--run", RUN])


def test_the_undo_action_is_a_mutating_action() -> None:
    """A reversal moves mail, so the digest must count it like any other mutation."""
    assert ledger.ACTION_UNDO in ledger.MUTATING_ACTIONS
