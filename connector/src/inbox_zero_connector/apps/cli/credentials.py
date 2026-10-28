"""The two verbs that own the stored credential: ``auth`` and ``logout``.

ONE MODULE BECAUSE IT IS ONE LIFECYCLE. ``auth`` puts a token in the OS
keychain and ``logout`` takes it out again; they share the provider vocabulary,
the install identity, and the same operator question -- what does this machine
hold, and on whose consent. Splitting them across files would put the grant and
the revocation out of each other's sight.

They live here rather than inline in ``main.py`` for the reason every other
verb in this directory does: a verb owns its parser (``add_parser``) and its
handler, and ``main.py`` stays a dispatch table. The move was forced by the
per-file code-line ceiling and is the shape the ceiling exists to produce.

Its stdout IS its product. The scope trade ``auth`` prints is the thing being
consented to, and printing it at the consent screen rather than in a doc read
later is the whole point; the logout prompt is likewise a question that has to
reach a human. CLI-only -- the server process never imports this module, so
nothing here can reach the MCP protocol channel on stdout.
"""

from __future__ import annotations

import argparse
import sys

from inbox_zero_connector.config import data_dir, install_id, load_config
from inbox_zero_connector.providers import get_provider
from inbox_zero_connector.services.auth import grants as grants
from inbox_zero_connector.services.auth import keychain as auth
from inbox_zero_connector.services.auth import scopes as scopes

PROVIDERS = scopes.PROVIDERS


def add_auth_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``auth`` row on the CLI's subparser action."""
    auth_parser = sub.add_parser("auth", help="Interactive OAuth bootstrap.")
    auth_parser.add_argument("provider", choices=PROVIDERS)


def add_logout_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``logout`` row on the CLI's subparser action.

    Two registration functions rather than one because ``build_parser`` keeps
    the two verbs at opposite ends of its help listing, and the listing is the
    operator-visible contract -- a lifecycle shared in code is not a reason to
    move a row in the printed order.
    """
    logout_parser = sub.add_parser("logout", help="Remove a stored credential.")
    logout_parser.add_argument("provider", choices=PROVIDERS)
    logout_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt. Required when stdin is not a terminal.",
    )


def _print_gmail_send_warning() -> None:
    """Say the interactive-Gmail send caveat at the consent screen itself.

    This is the moment the buyer is about to grant the scope, so the caveat is
    stated HERE, not only in a doc read later. The scope is named by printing
    ``scopes.GMAIL_MODIFY`` rather than by typing the string: a scope literal
    in this file would (correctly) trip the scope-home scan, which requires
    every scope string to live in ``scopes.py``. Printing the constant keeps
    the warning specific and the scan strict.
    """
    print()
    print(f"  WARNING: this request includes {scopes.GMAIL_MODIFY},")
    print("  the only interactive scope that can file mail -- and one that Gmail's")
    print("  send methods ALSO accept. This token CAN send. The no-send guarantee")
    print("  on interactive Gmail is code-layer: no send code path exists in this")
    print("  connector, and two scanners assert that every build. A scope-layer")
    print("  Gmail guarantee requires the DWD service-account path (see README);")
    print("  interactive consent cannot grant it (proven live 2026-08-18).")


def cmd_auth(provider_name: str) -> int:
    """Print the exact scope trade, then run the provider's interactive consent.

    The whole point of the printing is that consent is irreversible in the
    buyer's memory: what is requested, what is deliberately NOT requested, and
    which layer the no-send guarantee sits on are stated HERE, at the moment of
    granting, rather than only in a doc read later. The requested set comes
    from ``scopes.scopes_for`` and the drafts flag in the installed config, so
    the printout is a report of this deployment, not a fixed table.

    Args:
        provider_name: A key in ``PROVIDERS`` -- ``google`` or ``microsoft``.

    Returns:
        ``0`` after the credential is stored in the OS keychain.
    """
    drafts_on = load_config().drafts_on(provider_name)
    provider = get_provider(provider_name, drafts_enabled=drafts_on)
    requested = scopes.scopes_for(provider_name, drafts_on)

    print(f"Authenticating {provider_name} with exactly these scopes:")
    for scope in requested:
        print(f"  {scope}")
    print()
    print(f"Drafts enabled for {provider_name}: {drafts_on}")
    print(f"no-send enforcement: {scopes.enforcement_for(provider_name, drafts_on)}")
    if provider_name == "google":
        _print_gmail_send_warning()
    print()

    print("Not requested, and not usable with the resulting token:")
    excluded = tuple(
        scope
        for scope in (
            scopes.GMAIL_SEND_WOULD_REQUIRE
            if provider_name == "google"
            else scopes.GRAPH_SEND_WOULD_REQUIRE
        )
        if scope not in requested
    )
    for scope in excluded:
        print(f"  {scope}   (would be required to send mail)")
    print()

    account = provider.authenticate(interactive=True)
    # AFTER the consent returns, because this records that the grant HAPPENED
    # and a refused or abandoned consent must leave no timestamp. It is the
    # only moment the value is knowable: neither token carries the instant
    # consent was given, so the seven-day Testing window has no start date
    # unless it is captured right here (#64).
    grants.record_grant(data_dir(), provider_name)
    print(f"Authenticated as {account}.")
    print("Credential stored in the OS keychain. No token was written to disk.")
    return 0


def cmd_logout(provider_name: str, assume_yes: bool) -> int:
    """Confirm, then delete the provider's keychain credential.

    Deleting a credential that was never stored is not an error -- the operator
    asked for "no token here" and that is the end state either way -- so this
    reports which case it was rather than failing, and exits ``0`` for both.

    The CONFIRMATION is the part that is not politeness. Undoing this means the
    mailbox owner sitting through an interactive browser consent again, and the
    two things worth getting wrong are both invisible at the moment of typing:
    the wrong PROVIDER on a two-mailbox machine, and the wrong INSTALL when
    ``INBOX_ZERO_INSTALL_ID`` selects one of several. So the prompt names both.
    A non-interactive run cannot be prompted, so it is REFUSED rather than
    assumed -- ``--yes`` is how a script states the consent it cannot be asked
    for.

    Args:
        provider_name: A key in ``PROVIDERS`` -- ``google`` or ``microsoft``.
        assume_yes: Skip the prompt. Required when stdin is not a terminal.

    Returns:
        ``0`` when the delete ran, whether or not a credential existed to
        remove; ``1`` when it was declined or could not be confirmed.
    """
    if not assume_yes and not _confirm_logout(provider_name):
        return 1
    removed = auth.delete_token(provider_name)
    print(
        f"Removed the stored {provider_name} credential."
        if removed
        else f"No stored {provider_name} credential to remove."
    )
    return 0


def _confirm_logout(provider_name: str) -> bool:
    """Ask, naming the provider AND the install. ``False`` also means "could not ask".

    Args:
        provider_name: The credential about to be deleted.

    Returns:
        ``True`` only on an explicit ``y``/``yes``.
    """
    if not sys.stdin.isatty():
        print(
            f"Refusing to remove the {provider_name} credential without confirmation. "
            "Re-run with --yes to state the consent this non-interactive session "
            "cannot be asked for.",
            file=sys.stderr,
        )
        return False
    print(
        f"Remove the stored {provider_name} credential for install {install_id()}?\n"
        "Signing back in means another interactive browser consent."
    )
    if input("Type 'yes' to confirm: ").strip().lower() in {"y", "yes"}:
        return True
    print("Nothing was removed.")
    return False
