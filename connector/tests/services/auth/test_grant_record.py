"""When the grant was made, and what that implies about the refresh token (#64).

THE DEFECT THESE TESTS CLOSE. ``Health.token_expires_at`` shipped as a field
nothing could ever populate: the advance re-auth warning read it, found
``None`` on every install, and quietly never fired. The reason was not a bug in
the warning -- it was that no layer recorded WHEN a grant happened, and neither
token carries that instant. A Google access token's expiry is about an hour
out and renews itself; the seven-day Testing-mode window is measured from
consent, which is knowable only at consent.

So these tests assert the three facts that turn the dead arm live: the grant
time is written at auth, the expiry is derived from it under the one condition
where an expiry exists, and an install that predates the field keeps loading.

NO NETWORK, NO CREDENTIAL, NO KEYCHAIN. The grant record holds a timestamp and
a provider name, so every test here is a file and a clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from inbox_zero_connector.config import reset_settings
from inbox_zero_connector.services.auth.grants import (
    GOOGLE_TESTING_REFRESH_WINDOW,
    grants_path,
    record_grant,
    refresh_token_expiry,
)

GRANTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
"""A fixed consent instant, so the seven-day arithmetic is checkable by eye."""


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    """``refresh_token_expiry`` reads the app-status field, which is cached."""
    reset_settings()


def test_auth_records_when_the_grant_was_made(tmp_path: Path) -> None:
    """A successful auth leaves a grant time on disk. The whole start date.

    Asserted against the FILE rather than the returned record, because the
    return value would be satisfied by a function that computed a timestamp and
    persisted nothing -- and a value that does not survive the process is
    exactly as useless to the next watch round as no value at all.
    """
    record_grant(tmp_path, "google", now=GRANTED_AT)

    stored = json.loads(grants_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["google"]["granted_at"] == GRANTED_AT.isoformat()


def test_a_second_providers_grant_does_not_erase_the_first(tmp_path: Path) -> None:
    """Two mailboxes on one install re-authenticate independently.

    A rewrite from one provider's view would silently drop the other's expiry
    warning, which is invisible until the day that watcher stops.
    """
    record_grant(tmp_path, "google", now=GRANTED_AT)
    record_grant(tmp_path, "microsoft", now=GRANTED_AT + timedelta(days=1))

    stored = json.loads(grants_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["google"]["granted_at"] == GRANTED_AT.isoformat()


def test_testing_mode_expiry_is_the_grant_plus_seven_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Google Testing window, computed from the recorded consent instant."""
    monkeypatch.setenv("INBOX_ZERO_GOOGLE_APP_STATUS", "testing")
    reset_settings()
    record_grant(tmp_path, "google", now=GRANTED_AT)

    assert refresh_token_expiry("google", tmp_path) == GRANTED_AT + GOOGLE_TESTING_REFRESH_WINDOW


def test_a_published_app_has_no_predictable_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published refresh tokens do not expire on a clock, so there is nothing to warn about.

    ``None`` here is the correct answer and NOT a degraded one: a date invented
    for a published app would fire a re-auth warning at a healthy install every
    seven days, which is how an advance warning gets ignored.
    """
    monkeypatch.setenv("INBOX_ZERO_GOOGLE_APP_STATUS", "published")
    reset_settings()
    record_grant(tmp_path, "google", now=GRANTED_AT)

    assert refresh_token_expiry("google", tmp_path) is None


def test_an_old_grant_file_without_granted_at_still_loads(tmp_path: Path) -> None:
    """An install that authenticated before this field existed must keep working.

    The upgrade path is the case most likely to be missed and the most
    expensive to get wrong: a record shape that refused to parse would take out
    the health write on every existing install, turning a missing warning into
    a watcher that looks dead. It degrades to "no expiry known" instead.
    """
    grants_path(tmp_path).write_text(json.dumps({"google": {}}), encoding="utf-8")

    assert refresh_token_expiry("google", tmp_path) is None
