"""An UNLISTED container and an EXCLUDED one are two different failures (#198).

``_covered`` used to answer one boolean for both, so a message sitting in a
folder the provider's listing did not carry was tallied
``skipped_out_of_scope`` -- indistinguishable, in every count the watcher
keeps, from a folder the operator deliberately excluded. The operator was then
sent to edit a scope list that was already correct, and on ``all_folders``
there is no list to edit at all: the mail is in a folder nothing enumerated.

WHAT THIS MODULE PINS:

- an ``all_folders`` round whose mail sits in an id absent from
  ``list_containers`` tallies ``skipped_unenumerated_container``, NOT
  ``skipped_out_of_scope``;
- that round therefore does not arm ``saw_only_out_of_scope_mail``, because
  the scope refused nothing -- the LISTING did;
- the third streak WARN names the listing and points at ``list-folders``;
- a genuinely excluded container still tallies ``skipped_out_of_scope``, so
  the existing signal is unchanged;
- ``list-folders`` parses, dispatches, and prints one row per container with
  its coverage, using a fake provider and no network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from loguru import logger

from _watch_harness import CONFIDENTIAL_ID, INBOX_ID, FakeProvider, _context, _message
from inbox_zero_connector.apps.cli import list_folders as list_folders_cli
from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.providers.base import ChangeSet, Container
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.round import RoundState, RoundSummary
from inbox_zero_connector.services.watch.scope import MailboxScope

UNLISTED_ID = "Label_CreatedYesterday"
"""A container id the provider's listing does not carry. Ordinary: a folder
created, renamed, or hidden since the listing was taken."""

LISTING_MARKER = "container listing"
"""The phrase only the third WARN carries, so a test can name that signal."""


@pytest.fixture
def all_warnings() -> Iterator[list[str]]:
    """Every WARNING-or-above line loguru emitted, as plain strings.

    A loguru SINK rather than ``caplog``: this package logs through loguru,
    which does not propagate into the stdlib ``logging`` tree.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{message}")
    yield lines
    logger.remove(sink_id)


def _summary(**counts: int) -> RoundSummary:
    """A clean round's counts, overridden by whichever ones the caller names."""
    return RoundSummary(
        container_id=None,
        state=RoundState.COMMITTED,
        seen=counts.get("seen", 0),
        skipped_out_of_scope=counts.get("skipped_out_of_scope", 0),
        skipped_unenumerated_container=counts.get("skipped_unenumerated_container", 0),
        skipped_already_seen=counts.get("skipped_already_seen", 0),
        classified=counts.get("classified", 0),
        applied=counts.get("applied", 0),
        failed=counts.get("failed", 0),
        cursor_stored=True,
        resynced=False,
    )


def test_mail_in_an_unlisted_container_is_not_counted_out_of_scope() -> None:
    """``all_folders`` plus an unlisted id is a LISTING failure, not a scope one."""
    provider = FakeProvider(
        ChangeSet(messages=[_message("m-1", containers=(UNLISTED_ID,))], cursor="history-1")
    )
    context = _context(provider, scope=MailboxScope(mode="all_folders"))

    summary = watcher.poll_once(context)

    assert summary.skipped_unenumerated_container == 1
    assert summary.skipped_out_of_scope == 0
    assert summary.classified == 0


def test_an_excluded_container_is_still_counted_out_of_scope() -> None:
    """The scope genuinely refusing a LISTED folder keeps the existing count."""
    provider = FakeProvider(
        ChangeSet(messages=[_message("m-1", containers=(CONFIDENTIAL_ID,))], cursor="history-1")
    )
    context = _context(
        provider,
        scope=MailboxScope(mode="all_except", excluded_folders=("Client Matters",)),
    )

    summary = watcher.poll_once(context)

    assert summary.skipped_out_of_scope == 1
    assert summary.skipped_unenumerated_container == 0


def test_an_unenumerated_round_does_not_arm_the_scope_alarm() -> None:
    """The scope refused nothing, so the scope streak must not claim it did."""
    unenumerated = watchloop.PassResult(
        streams=(
            watchloop.StreamResult(
                None, "COMMITTED", _summary(seen=1, skipped_unenumerated_container=1)
            ),
        )
    )

    assert unenumerated.saw_only_out_of_scope_mail is False


def test_the_listing_streak_warns_and_points_at_list_folders(all_warnings: list[str]) -> None:
    """The third signal names the LISTING and gives the operator the command."""
    provider = FakeProvider(ChangeSet())
    context = _context(provider, scope=MailboxScope(mode="all_folders"))
    unenumerated = watchloop.PassResult(
        streams=(
            watchloop.StreamResult(
                None, "COMMITTED", _summary(seen=1, skipped_unenumerated_container=1)
            ),
        )
    )

    streaks = watchloop._SilenceStreaks()
    for _ in range(watchloop.ZERO_STREAM_WARN_ROUNDS):
        streaks.record(unenumerated)
        streaks.warn_if_due(context)

    listing_lines = [line for line in all_warnings if LISTING_MARKER in line]
    assert len(listing_lines) == 1, f"one listing WARN, not {all_warnings}"
    assert "list-folders" in listing_lines[0]


class _ScopedListingProvider(FakeProvider):
    """A mailbox whose listing is the whole product of ``list-folders``."""

    def __init__(self) -> None:
        super().__init__(ChangeSet())


def test_list_folders_prints_every_container_and_its_coverage(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One row per container: id, display name, and whether scope covers it."""
    monkeypatch.setattr(
        list_folders_cli, "get_provider", lambda *args, **kwargs: _ScopedListingProvider()
    )

    assert cli.main(["list-folders"]) == 0

    out = capsys.readouterr().out
    assert INBOX_ID in out
    assert "Client Matters" in out
    inbox_line = next(line for line in out.splitlines() if INBOX_ID in line)
    assert "covered" in inbox_line


def test_list_folders_is_a_registered_verb() -> None:
    """The parser row exists, so the command is reachable without the provider."""
    args = cli.build_parser().parse_args(["list-folders"])
    assert args.command == "list-folders"


def test_a_listed_container_is_reported_uncovered_when_the_scope_excludes_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage is read from the installed scope, never assumed."""
    monkeypatch.setattr(
        list_folders_cli, "get_provider", lambda *args, **kwargs: _ScopedListingProvider()
    )
    monkeypatch.setattr(
        list_folders_cli,
        "_installed_scope",
        lambda _provider_name: MailboxScope(
            mode="all_except", excluded_folders=("Client Matters",)
        ),
    )

    assert cli.main(["list-folders"]) == 0

    out = capsys.readouterr().out
    confidential_line = next(line for line in out.splitlines() if CONFIDENTIAL_ID in line)
    assert "NOT covered" in confidential_line


def test_the_unlisted_container_is_absent_from_the_fake_listing() -> None:
    """Guards the fixture itself: the id under test must really be missing."""
    listed = {container.id for container in FakeProvider(ChangeSet()).list_containers()}
    assert UNLISTED_ID not in listed
    assert (
        Container(
            id=UNLISTED_ID,
            name="Created Yesterday",
            well_known=None,
            parent_id=None,
            exclusive=False,
        ).id
        not in listed
    )
