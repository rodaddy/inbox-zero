"""``status`` renders each of the three watcher states off a written health file.

The states are the product: an operator reads one word and knows whether to
look at the log, the service manager, or nothing. Each is asserted with the
exit code beside it, because the exit code is the half of the answer a
scheduler consumes and the two must never disagree -- a run that PRINTS
``failing`` and EXITS 0 is a monitor that reports the outage as healthy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from inbox_zero_connector.apps.cli.status import cmd_status
from inbox_zero_connector.config_paths import DATA_DIR_ENV
from inbox_zero_connector.services.watch.health import Health, write_health


@pytest.fixture
def data_directory(tmp_path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Point the data directory at a temp path so no real install is read."""
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    return tmp_path


def test_no_health_file_exits_three(data_directory, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    """Nothing to read is not "unhealthy" -- it is "cannot answer" (exit 3)."""
    assert cmd_status() == 3
    assert "No health file yet" in capsys.readouterr().out


def test_recent_clean_round_is_healthy(data_directory, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    """A success inside the staleness window and no errors exits 0."""
    now = datetime.now(UTC).isoformat()
    write_health(data_directory, Health(last_round_at=now, last_success_at=now))

    assert cmd_status(interval_seconds=60.0) == 0
    assert "Watcher: healthy" in capsys.readouterr().out


def test_old_success_is_stale(data_directory, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    """Past 2x the interval with no error, the watcher is not running."""
    old = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
    write_health(data_directory, Health(last_round_at=old, last_success_at=old))

    assert cmd_status(interval_seconds=60.0) == 1
    assert "Watcher: stale" in capsys.readouterr().out


def test_errored_round_is_failing_and_names_the_type(
    data_directory, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    """Rounds are happening and erroring: failing, not stale, with the count."""
    now = datetime.now(UTC).isoformat()
    write_health(
        data_directory,
        Health(
            last_round_at=now,
            last_success_at=now,
            last_error="TimeoutError",
            consecutive_failures=4,
        ),
    )

    assert cmd_status(interval_seconds=60.0) == 1
    out = capsys.readouterr().out
    assert "Watcher: failing" in out
    assert "Consecutive failures: 4 (TimeoutError)" in out


def test_imminent_expiry_prints_the_reauth_date_and_needs_attention(
    data_directory, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    """A watcher whose credential dies tomorrow reads needs attention and exits 1."""
    now = datetime.now(UTC)
    expires = now + timedelta(hours=6)
    write_health(
        data_directory,
        Health(
            last_round_at=now.isoformat(),
            last_success_at=now.isoformat(),
            token_expires_at=expires.isoformat(),
        ),
    )

    assert cmd_status(interval_seconds=60.0) == 1
    out = capsys.readouterr().out
    assert "Watcher: needs attention" in out
    assert f"re-auth needed by {expires.date().isoformat()}" in out


def test_past_expiry_reads_overdue_not_a_future_deadline(
    data_directory, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    """A closed refresh window is stated in the past tense, not as a due date."""
    now = datetime.now(UTC)
    expires = now - timedelta(days=2)
    write_health(
        data_directory,
        Health(
            last_round_at=now.isoformat(),
            last_success_at=now.isoformat(),
            token_expires_at=expires.isoformat(),
        ),
    )

    assert cmd_status(interval_seconds=60.0) == 1
    out = capsys.readouterr().out
    assert (
        f"re-auth OVERDUE since {expires.date().isoformat()}: "
        "the watcher cannot refresh its token" in out
    )
    assert "needed by" not in out


def test_reauth_line_never_prints_beside_the_word_healthy(
    data_directory, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    """The state word and the exit code agree: a 1 never reads ``healthy``."""
    now = datetime.now(UTC)
    write_health(
        data_directory,
        Health(
            last_round_at=now.isoformat(),
            last_success_at=now.isoformat(),
            token_expires_at=(now + timedelta(hours=6)).isoformat(),
        ),
    )

    assert cmd_status(interval_seconds=60.0) == 1
    out = capsys.readouterr().out
    assert "Watcher: needs attention" in out
    assert "healthy" not in out


def test_sub_second_age_reads_just_now(data_directory, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    """Under a second is "just now" -- "0s ago" reads like a broken clock."""
    now = datetime.now(UTC).isoformat()
    write_health(data_directory, Health(last_round_at=now, last_success_at=now))

    assert cmd_status(interval_seconds=60.0) == 0
    assert "Last successful run: just now" in capsys.readouterr().out
