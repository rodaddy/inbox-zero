"""Tests for the ``rules`` CLI subcommand: list and disable.

No mail, no model, no network, no credential. The learned-rule store is a JSON
file, so the whole surface under test here is "read the table, render it" and
"turn one row off, save it". The autouse ``isolated_dirs`` fixture in
``conftest.py`` redirects ``INBOX_ZERO_CONFIG_DIR`` at a ``tmp_path``, so
``learned_rules_path()`` resolves inside the test's own sandbox and no test can
read or write a real deployment's learned rules.

The CLI is exercised through ``cli.main`` rather than by calling the ``_cmd_*``
handlers directly, which is what makes these tests prove the parser rows, the
dispatch table, and the exit codes together -- the same boundary
``test_reset.py`` uses.

Rule CREATION is deliberately absent from the CLI and therefore from these
tests: only an observed owner correction writes a row, so ``rules`` is
list/disable only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.services.triage.learned import (
    LearnedRule,
    Predicate,
    learned_rules_path,
    load_rules,
    save_rules,
)

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)


def _rule(rule_id: int, value: str, folder: str, **overrides: object) -> LearnedRule:
    """One row, with only the fields a test cares about spelled out."""
    fields: dict[str, object] = {
        "id": rule_id,
        "predicate": Predicate.SENDER_IS,
        "value": value,
        "folder": folder,
        "learned_at": NOW.isoformat(),
        "source_message_id": f"msg-{rule_id}",
    }
    fields.update(overrides)
    return LearnedRule(**fields)  # type: ignore[arg-type]


def test_list_empty_store_is_honest_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """An empty table prints one honest line rather than nothing at all."""
    assert cli.main(["rules", "list"]) == 0
    assert "no learned rules" in capsys.readouterr().out


def test_list_renders_every_rule_with_id_and_folder(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both rows are rendered, and the id and folder are visible on each."""
    save_rules((
        _rule(1, "billing@acme.com", "Finance"),
        _rule(2, "news@example.com", "Newsletters"),
    ))

    assert cli.main(["rules", "list"]) == 0

    out = capsys.readouterr().out
    assert "billing@acme.com" in out
    assert "Finance" in out
    assert "news@example.com" in out
    assert "Newsletters" in out
    assert "1" in out
    assert "2" in out


def test_disable_known_id_writes_enabled_false_to_disk(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The disable lands in the FILE, not just in the printed row.

    Asserting on disk rather than on stdout is the point: a command that
    printed a disabled row but never saved would pass a stdout-only test and
    lose the owner's correction on the next load.
    """
    save_rules((_rule(1, "billing@acme.com", "Finance"),))

    assert cli.main(["rules", "disable", "1", "--reason", "no longer files here"]) == 0

    payload = json.loads(learned_rules_path().read_text(encoding="utf-8"))
    assert payload["rules"][0]["enabled"] is False
    assert payload["rules"][0]["disabled_reason"] == "no longer files here"
    assert "no longer files here" in capsys.readouterr().out


def test_disable_defaults_the_reason_when_none_is_given() -> None:
    """A bare disable still records WHY, so no row is off for no stated reason."""
    save_rules((_rule(1, "billing@acme.com", "Finance"),))

    assert cli.main(["rules", "disable", "1"]) == 0

    (row,) = load_rules()
    assert row.enabled is False
    assert row.disabled_reason == "disabled by operator"


def test_disable_unknown_id_exits_one_and_names_known_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ValueError text reaches stderr, so the owner learns which ids exist."""
    save_rules((_rule(1, "billing@acme.com", "Finance"),))

    assert cli.main(["rules", "disable", "99"]) == 1

    captured = capsys.readouterr()
    assert "99" in captured.err
    assert "[1]" in captured.err


def test_disable_leaves_the_other_rows_alone() -> None:
    """Disabling one row is not a rewrite of the table."""
    save_rules((
        _rule(1, "billing@acme.com", "Finance"),
        _rule(2, "news@example.com", "Newsletters"),
    ))

    assert cli.main(["rules", "disable", "1"]) == 0

    first, second = load_rules()
    assert first.enabled is False
    assert second.enabled is True
    assert second.folder == "Newsletters"


def test_list_marks_a_disabled_row_and_shows_its_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A disabled row reads as disabled, with the reason it was turned off."""
    save_rules((
        _rule(
            1, "billing@acme.com", "Finance", enabled=False, disabled_reason="corrected by owner"
        ),
    ))

    assert cli.main(["rules", "list"]) == 0

    out = capsys.readouterr().out
    assert "DISABLED" in out
    assert "corrected by owner" in out


def test_parser_round_trips_both_shipped_spellings() -> None:
    """Both the positional actions and the parent-parser flags parse.

    The sprint demo text uses ``rules --list`` / ``rules --disable 1``; the
    house pattern is a positional action subparser. Both are shipped, so both
    are asserted here -- a demo that stopped parsing would otherwise only be
    discovered by running the demo.
    """
    parser = cli.build_parser()

    positional_list = parser.parse_args(["rules", "list"])
    assert positional_list.command == "rules"
    assert positional_list.rules_action == "list"

    positional_disable = parser.parse_args(["rules", "disable", "7", "--reason", "why"])
    assert positional_disable.rules_action == "disable"
    assert positional_disable.rule_id == 7
    assert positional_disable.reason == "why"

    flag_list = parser.parse_args(["rules", "--list"])
    assert flag_list.command == "rules"
    assert flag_list.list_flag is True

    flag_disable = parser.parse_args(["rules", "--disable", "1"])
    assert flag_disable.disable_id == 1


def test_flag_spellings_behave_like_the_positional_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--list`` and ``--disable N`` do the same work as the positional forms."""
    save_rules((_rule(1, "billing@acme.com", "Finance"),))

    assert cli.main(["rules", "--list"]) == 0
    assert "billing@acme.com" in capsys.readouterr().out

    assert cli.main(["rules", "--disable", "1"]) == 0
    (row,) = load_rules()
    assert row.enabled is False


def test_bare_rules_with_no_action_lists() -> None:
    """``rules`` alone is a read, which is the safe default for a bare command."""
    save_rules((_rule(1, "billing@acme.com", "Finance"),))
    assert cli.main(["rules"]) == 0
