"""The ``list-folders`` subcommand: what this install can actually enumerate.

Lives outside ``main.py`` for the same reason ``show_scopes.py`` and
``desktop.py`` do: that module carries the parser and dispatch table for every
verb and sits against the repo's code-line ceiling, and this verb is a
self-contained render with no shared state.

IT EXISTS BECAUSE A REFUSED MESSAGE HAS TWO CAUSES AND THEY LOOK IDENTICAL
(#198). The watcher refuses a message whose container the provider's listing
did not carry, exactly as it refuses one the scope excludes -- both are
unreadable placements and neither may resolve to permission. The operator's fix
is not the same, though: the second is a folder name in the config, and the
first is a folder the connector cannot see at all, which no scope edit can
reach. The watcher's listing WARN names this command; this is the command that
answers it, by printing the listing the round itself reads and the coverage the
installed scope computes over it.

Its stdout IS its product -- a listing cannot be delivered through the logger,
which writes to sinks -- and it never runs in the server process, so nothing it
prints can reach the MCP protocol channel.

NO NETWORK AT IMPORT. The provider is constructed inside ``cmd_list_folders``,
never at module scope, so importing this module costs nothing and the cold
import test stays true. Folder ids and display names are configuration, not
mail content, so printing them leaks nothing the scope manifest does not.
"""

from __future__ import annotations

import argparse

from inbox_zero_connector.config import load_settings
from inbox_zero_connector.providers import get_provider
from inbox_zero_connector.providers.base import Container
from inbox_zero_connector.services.watch.scope import MailboxScope

COVERED = "covered"
NOT_COVERED = "NOT covered"
"""The two coverage words, uppercased on the refusing side so a scan finds it."""


def _installed_scope(provider_name: str) -> MailboxScope:
    """Return the mailbox scope this install actually runs with.

    A named function rather than an inline read so the whole scope decision has
    ONE source here and a test can substitute an install without writing a
    config file.

    Args:
        provider_name: The active provider, as ``load_settings`` reports it.

    Returns:
        The installed ``MailboxScope`` for that provider.
    """
    return load_settings().connector_config().mailbox_scope(provider_name)


def _coverage(scope: MailboxScope, container: Container) -> str:
    """Say whether the scope covers this one container, in the operator's words.

    Delegates to ``MailboxScope.covers`` and decides nothing: a second
    implementation of the scope decision is a second chance to disagree with
    the watcher, and this command's whole value is agreeing with it.

    Args:
        scope: The installed scope.
        container: One enumerated container.

    Returns:
        ``COVERED`` or ``NOT_COVERED``.
    """
    if scope.covers(container.name, well_known=container.well_known):
        return COVERED
    return NOT_COVERED


def cmd_list_folders() -> int:
    """Print every container this install can enumerate, with its coverage.

    One row per container -- id, display name, coverage -- because the operator
    reading this is holding the mailbox in the other window and is looking for a
    folder that is NOT in the list. A folder present in the mailbox and absent
    here is the #198 condition itself, and the closing line says so rather than
    leaving the absence to be interpreted.

    Returns:
        ``0``. A listing that raises propagates to the CLI's refusal handler,
        which is where an unauthenticated install is already rendered.
    """
    active = load_settings()
    provider_name = active.provider
    scope = _installed_scope(provider_name)
    provider = get_provider(
        provider_name,
        drafts_enabled=active.connector_config().drafts_on(provider_name),
    )
    containers = provider.list_containers()

    print(f"{provider_name}: scope mode={scope.mode}, folders enumerated={len(containers)}\n")
    for container in containers:
        print(f"{container.id}\t{container.name}\t{_coverage(scope, container)}")
    print()
    print("A folder in this mailbox but NOT in this list cannot be enumerated by this")
    print("install, so mail sitting in it is refused before the scope is consulted --")
    print("editing folder names in the connector config cannot reach it.")
    return 0


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``list-folders`` verb on the CLI's subparser action.

    The parser row lives HERE rather than inline in ``build_parser`` for the
    same reason ``init_config.add_parser`` does: ``main.py`` sits against the
    repo's file and function code-line ceilings, and a verb that owns its own
    module can own its own row.

    Args:
        sub: The subparser action every verb is registered on.
    """
    sub.add_parser(
        "list-folders",
        help="Print every folder this install can enumerate and whether scope covers it.",
    )
