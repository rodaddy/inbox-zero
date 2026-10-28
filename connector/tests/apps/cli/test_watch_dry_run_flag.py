"""``watch --dry-run`` is refused without ``--once`` (#66 part 1).

A CONTINUOUS PREVIEW IS THE FAILURE THIS REFUSAL PREVENTS, not a style rule. A
dry round withholds its cursor by design, so a daemon running one would re-read
the same mail every interval, print the same table forever, and never file
anything -- a watcher that looks alive beside an inbox that never clears, which
is precisely the silent-round condition the connector's own warnings exist to
catch. Accepting the combination would manufacture it on purpose.

The refusal is checked at the handler rather than through argparse, because
argparse cannot express "this flag requires that flag" without a mutually
exclusive group that would also forbid the combination that IS legal.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.cli import dry_run_preview
from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.providers.base import Container, MailProvider
from inbox_zero_connector.services.watch.preview import PlannedFiling


class ListingProvider(MailProvider):
    """A mailbox that reports a fixed folder listing. Every write raises.

    The renderer's only provider call is ``list_containers``; anything else
    reaching this class is the defect the preview exists to prevent, so it
    fails loudly rather than reading as a pass.
    """

    name = "google"

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names

    def list_containers(self) -> list[Container]:
        return [
            Container(id=f"id-{name}", name=name, well_known=None, parent_id=None, exclusive=False)
            for name in self.names
        ]

    def _unreachable(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("the dry-run preview must not touch the mailbox")

    authenticate = _unreachable
    create_draft = _unreachable
    create_self_draft = _unreachable
    file_message = _unreachable
    get_thread = _unreachable
    list_inbox = _unreachable
    poll_changes = _unreachable
    read_sent_sample = _unreachable
    set_important = _unreachable
    tag_message = _unreachable


def _plan(message_id: str, folder: str) -> PlannedFiling:
    return PlannedFiling(message_id=message_id, folder=folder, deterministic=False)


def test_a_folder_the_mailbox_lacks_is_marked_would_create(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE POINT OF #66: the operator sees the new folders BEFORE they exist.

    Both adapters create folders lazily inside their own filing call, so the
    first real pass builds them in the mailbox with no warning. This is that
    warning, and it must distinguish a folder that already exists from one that
    does not -- marking everything would train the operator to ignore it.
    """
    dry_run_preview.render(
        (_plan("m-1", "Business-Action"), _plan("m-2", "Review")),
        ListingProvider(("Inbox", "Review")),
    )

    out = capsys.readouterr().out
    assert "m-1 -> Business-Action" in out
    assert "m-2 -> Review" in out
    business = next(line for line in out.splitlines() if "m-1" in line)
    review = next(line for line in out.splitlines() if "m-2" in line)
    assert dry_run_preview.WOULD_CREATE in business, "a missing folder must be marked"
    assert dry_run_preview.WOULD_CREATE not in review, "an existing folder must not be"


def test_the_preview_prints_no_message_content(capsys: pytest.CaptureFixture[str]) -> None:
    """Identifiers only. This output is screenshotted into support threads.

    ``PlannedFiling`` carries no content by construction, so this asserts the
    RENDERER adds none of its own -- the failure it guards is a future line
    that reaches for a message to make the table friendlier.
    """
    dry_run_preview.render((_plan("m-1", "Review"),), ListingProvider(("Review",)))

    out = capsys.readouterr().out
    assert "Quarterly" not in out
    assert "@" not in out, "an address in the preview is mailbox content on a terminal"


def test_dry_run_without_once_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 1, say why on stderr, and never build a provider or touch the mailbox.

    Reaching ``load_settings`` would be the defect: the refusal must land
    before any install is read, so a machine with no credential still gets the
    usage error rather than an auth failure that hides it.
    """
    assert cli._cmd_watch(60.0, once=False, dry_run=True) == 1

    captured = capsys.readouterr()
    assert "--dry-run requires --once" in captured.err
    assert "watch --once --dry-run" in captured.err, "the error must name the fix"


def test_the_watch_parser_accepts_dry_run_and_defaults_it_off() -> None:
    """The flag exists, and a watch invoked without it is a REAL pass.

    The default is the safety property: a dry default would make every existing
    caller a silent no-op, which is a worse failure than the one the flag
    closes because nothing would look wrong.
    """
    parser = cli.build_parser()
    assert parser.parse_args(["watch", "--once"]).dry_run is False
    assert parser.parse_args(["watch", "--once", "--dry-run"]).dry_run is True
