"""Render one ``watch --once --dry-run`` preview, and say which folders are new.

WHY THIS EXISTS. Both adapters create the taxonomy's folders LAZILY, inside
their own filing call (``providers/gmail/provider.py`` ``_create_label``,
``providers/outlook/provider.py`` ``create_folder``), so the first real watch
pass against a live mailbox builds folders in the owner's mail client with no
warning beforehand. The operator's first sight of the taxonomy was finding it
already there. This renderer is the warning that was missing: it prints what
the round WOULD file and marks every destination the mailbox does not already
have as ``WOULD CREATE``.

THE MAILBOX COMPARISON IS A READ, AND IT IS THE ONLY PROVIDER CALL HERE.
``list_containers`` is the same listing ``list-folders`` prints and the same
one the round itself reads to resolve placements. Nothing in this module
writes, and the round it renders wrote nothing either -- see
``services/watch/round.py`` ``RoundContext.dry_run``.

IDENTIFIERS ONLY, ON A CHANNEL THAT IS EASY TO FORGET ABOUT. The rule in
``utils/logging_config.py`` -- never a body, subject, address, or token -- is
written for the log sinks, and this output is a terminal an operator will
screenshot into a support thread, which is a copy in a place with different
permissions and a different lifetime for exactly the same reason a log file is.
``PlannedFiling`` carries no message content by construction, so this module
cannot print any: the only strings it has are a provider id, a taxonomy folder
name the operator chose, and its own literals.

Its stdout IS its product, like every other module in this package's CLI
directory, and it never runs in the server process, so nothing it prints can
reach the MCP protocol channel.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from inbox_zero_connector.providers.base import MailProvider

if TYPE_CHECKING:
    # DEFERRED FOR THE SAME REASON ``main.py`` defers the watch loop: the watch
    # engine is not on the cold import path of every CLI verb, and pulling it
    # in at module scope for a type annotation would put it there
    # (``tests/apps/cli/test_cold_import.py``). Nothing here calls it.
    from inbox_zero_connector.services.watch.loop import PassResult
    from inbox_zero_connector.services.watch.round import PlannedFiling

WOULD_CREATE = "WOULD CREATE"
"""Marker for a destination the mailbox does not have yet. Uppercase so an
operator skimming the table finds the consequential rows without reading it,
and so a support thread can be grepped for the word."""


def _existing_folder_names(provider: MailProvider) -> set[str]:
    """Every container name the mailbox reports, for the WOULD CREATE test.

    NAMES, NOT IDS. A planned filing carries the taxonomy folder the classifier
    chose, which is a display name; the listing is what maps names to the
    mailbox's own ids. Matching on the name is therefore the only comparison
    available at this boundary, and it is the same comparison the adapters make
    when they decide whether to create the folder -- so this preview answers
    the question the real pass will actually ask.

    Args:
        provider: The constructed provider. Read only.

    Returns:
        The set of container display names the mailbox currently has.
    """
    return {container.name for container in provider.list_containers()}


def render(planned: tuple[PlannedFiling, ...], provider: MailProvider) -> None:
    """Print the preview table and the folder-creation warning. No mutation.

    An EMPTY preview is printed as such rather than silently producing nothing:
    a dry run that found no mail and a dry run that crashed before printing
    look identical on a blank terminal, and only one of them means the mailbox
    is quiet.

    Args:
        planned: The filings the dry round decided on, from
            ``RoundSummary.planned``. Identifiers only.
        provider: The provider to read the current folder listing from. Never
            written to.
    """
    if not planned:
        print("Dry run: no messages to file. Nothing would be created.")
        return
    existing = _existing_folder_names(provider)
    # Computed BEFORE the loop so a folder planned twice is announced once as
    # new -- the real pass creates it on the first filing, and a second
    # WOULD CREATE beside the same name reads as two folders.
    would_create = sorted({plan.folder for plan in planned if plan.folder not in existing})
    print(f"Dry run: {len(planned)} message(s) would be filed. Nothing was changed.")
    for plan in planned:
        marker = f"  [{WOULD_CREATE}]" if plan.folder in would_create else ""
        print(f"  {plan.message_id} -> {plan.folder}{marker}")
    if not would_create:
        print("\nEvery destination folder already exists in the mailbox.")
        return
    print(
        f"\n{WOULD_CREATE}: {len(would_create)} folder(s) do not exist yet and the "
        "first real pass would create them in the mailbox:"
    )
    for folder in would_create:
        print(f"  {folder}")


def exit_code(result: PassResult | None, provider: MailProvider) -> int:
    """Print the preview and return 0 when it ran. Never mutates anything.

    THE PREVIEW HAS ITS OWN EXIT CONTRACT, and it is not ``--once``'s.
    ``main._once_exit_code`` returns 0 only for a pass that FILED cleanly, and a
    dry pass files nothing on purpose -- routed through it, a successful preview
    of a busy mailbox would exit 0 by accident of the counts rather than by
    contract, and a preview of an empty one would exit 1 for having polled a
    stream that had nothing to show. The question this run answers is "did the
    preview run", so that is what its status reports.

    Args:
        result: What ``loop.run`` returned -- ``None`` when a stop landed before
            any pass ran, which is the one case there is nothing to preview.
        provider: The constructed provider, read for the folder listing the
            ``WOULD CREATE`` comparison needs. Never written to.

    Returns:
        ``0`` when a pass ran and its preview was printed, ``1`` when no pass
        ran or the configured scope selected no stream -- the same two "nothing
        was observed" conditions ``main._once_exit_code`` refuses, and for the
        same reason: an operator reading an empty preview must not be told the
        mailbox was checked and found tidy when it was never read.
    """
    if result is None or result.streams_polled == 0:
        print(
            "Dry run observed nothing: no pass ran, or the configured mailbox "
            "scope selected no stream. Check the connector config's mailbox "
            "scope for this provider, then run list-folders to see what this "
            "install can enumerate.",
            file=sys.stderr,
        )
        return 1
    planned = tuple(
        plan
        for stream in result.streams
        if stream.summary is not None
        for plan in stream.summary.planned
    )
    render(planned, provider)
    return 0
