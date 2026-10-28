"""The ``uninstall`` subcommand: a GUIDED offboarder, not an uninstaller (#68).

The install is one scripted command. Removal was memory -- and an install you
can only take down from memory is the one that does not get taken down. That
asymmetry has a specific cost on a shared mailbox: an old machine nobody
offboarded keeps its supervisor restarting a watch, which is exactly the second
watcher the one-watcher-per-mailbox rule forbids, and nothing detects it. The
symptom is duplicate labels and two ledgers that each look like the audit
trail; the cause is a laptop in a drawer.

WHAT IT DOES AND WHAT IT REFUSES TO DO. There is exactly one piece of teardown
that is deterministic, local, and reversible by re-consent -- the OS keychain
credential -- and this command does that one thing. Everything else is PRINTED
with the exact command or URL:

* the SERVICE is print-only, matching ``service-unit``'s posture on the way in.
  Placing a supervisor unit is a decision about the operator's own machine, so
  removing one is too, and a command that boots out a service it did not
  install is guessing at which login session it belongs to. The unit NAMES come
  from ``service_unit`` by import rather than by retyping: two copies of a
  launchd label drift the moment one is edited, and the failure mode is an
  operator running ``bootout`` on a label that does not exist and reading the
  error as "already gone".
* REVOCATION is the provider's, not ours. A local token delete stops THIS
  machine from using the grant; it does not withdraw the grant, which continues
  to sit in the owner's Google or Microsoft account until they remove it there.
  Saying "uninstalled" without saying that would leave an owner believing an
  access they can still see in their account settings had been withdrawn.
* FILES ARE NEVER DELETED. The command prints the ``rm`` lines for the human.
  That is the product's posture and this repo's law agreeing, and it is also
  simply correct: the ledger is an audit trail, and an audit trail an agent can
  remove on its own is not one.

AND WHAT STAYS, SAID OUT LOUD. Labels and filed mail remain in the mailbox by
design -- the connector files into the owner's own folders, and offboarding it
does not un-file anything. Unfiling and label deletion are explicit non-goals
(#68): the owner's mail is theirs, and a tool that reorganised a mailbox on its
way out would be doing the single most destructive thing it is capable of at
the exact moment nobody is watching it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Final

from inbox_zero_connector.config import (
    config_dir,
    config_path,
    data_dir,
    install_id,
    ledger_path,
    secrets_path,
    taxonomy_path,
)
from inbox_zero_connector.services.auth import keychain as auth

from .service_unit import _LABEL_PREFIX

LABEL_PREFIX: Final = _LABEL_PREFIX
"""The launchd label prefix, REUSED from ``service_unit`` rather than retyped.

Re-exported under a public name so this module's callers and its tests can
assert the identity without reaching through another module's underscore.
"""

REVOCATION_URLS: Final[dict[str, str]] = {
    "google": "https://myaccount.google.com/permissions",
    "microsoft": "https://account.live.com/consent/Manage",
}
"""Where the OWNER withdraws the grant, per provider.

Deleting the local token is not revocation and this connector cannot perform
one: the grant lives in the owner's own account, and the OAuth flow that
created it deliberately never gave this machine an administrative handle on it.
A work or school account may have its consent managed by the tenant instead,
which is why the printed text names MyApps as the alternative rather than
presenting the personal-account URL as the only answer.
"""


def _print_service_teardown(provider: str) -> None:
    """Print the exact bootout / disable lines, and report a plist if one is here.

    Print-only for the reason ``service-unit`` is print-only on the way in. The
    detection is worth doing anyway: it is the difference between an operator
    reading a generic instruction and one being told "yes, there is a unit on
    this machine, here it is".
    """
    plist = f"{LABEL_PREFIX}.{provider}.plist"
    print("1. STOP THE SUPERVISOR (run these yourself; this command loads nothing).")
    print(f"   macOS:  launchctl bootout gui/$(id -u)/{LABEL_PREFIX}.{provider}")
    print(f"           rm ~/Library/LaunchAgents/{plist}")
    print(f"   Linux:  systemctl --user disable --now inbox-zero-connector-{provider}.service")
    print(f"           rm ~/.config/systemd/user/inbox-zero-connector-{provider}.service")

    from pathlib import Path

    installed = Path.home() / "Library" / "LaunchAgents" / plist
    if installed.is_file():
        print(f"   FOUND on this machine: {installed}")
    print()


def _print_revocation(provider: str) -> None:
    """Print where the owner withdraws the grant, and say plainly that we cannot."""
    print("3. REVOKE THE GRANT (only the mailbox owner can do this).")
    print("   This connector cannot revoke server-side access: the grant lives in")
    print("   the owner's account, not on this machine, and the local token delete")
    print("   above stops THIS machine using it without withdrawing it.")
    print(f"   {REVOCATION_URLS[provider]}")
    if provider == "microsoft":
        print("   Work or school account: the tenant's My Apps page instead --")
        print("   https://myapplications.microsoft.com")
    print()


def _print_what_stays() -> None:
    """Print the files that remain, the rm lines for a human, and the mail promise."""
    print("4. WHAT STAYS, AND WHAT TO DO ABOUT IT.")
    print("   Labels and filed mail REMAIN in the mailbox, by design. This tool")
    print("   files into the owner's own folders and does not un-file on the way")
    print("   out; nothing is reorganised or deleted in the mailbox.")
    print()
    print(f"   These local files remain (install id: {install_id() or '(none)'}):")
    for path in (taxonomy_path(), config_path(), secrets_path(), ledger_path()):
        print(f"     {path}")
    print()
    print("   To remove them, run these YOURSELF -- this command deletes no file:")
    print(f"     rm -rf {config_dir()}")
    print(f"     rm -rf {data_dir()}")
    print()


def _confirm(provider: str) -> bool:
    """Ask, naming provider and install. ``False`` also means "could not ask".

    Same contract as ``logout`` and for the same reason: undoing a credential
    delete means the mailbox owner sitting through an interactive browser
    consent again, and the two things worth getting wrong -- the wrong PROVIDER
    on a two-mailbox machine, the wrong INSTALL when an id selects one of
    several -- are both invisible at the moment of typing.
    """
    if not sys.stdin.isatty():
        print(
            f"Refusing to offboard the {provider} install without confirmation. "
            "Re-run with --yes to state the consent this non-interactive session "
            "cannot be asked for.",
            file=sys.stderr,
        )
        return False
    print(
        f"Offboard the {provider} install {install_id() or '(un-namespaced)'}?\n"
        "This removes the stored credential. Signing back in means another "
        "interactive browser consent."
    )
    if input("Type 'yes' to confirm: ").strip().lower() in {"y", "yes"}:
        return True
    print("Nothing was removed.")
    return False


def cmd_uninstall(provider: str, assume_yes: bool) -> int:
    """Delete the credential, then print every step this command will not take.

    Args:
        provider: A provider key -- ``google`` or ``microsoft``.
        assume_yes: Skip the prompt. Required when stdin is not a terminal.

    Returns:
        ``0`` when the guided offboarding ran, ``1`` when it was declined or
        could not be confirmed.
    """
    if not assume_yes and not _confirm(provider):
        return 1

    print(f"Offboarding the {provider} install. Deleting one credential; nothing else.")
    print()
    _print_service_teardown(provider)

    removed = auth.delete_token(provider)
    print(
        "2. LOCAL CREDENTIAL: " + ("removed from the OS keychain." if removed else "none stored.")
    )
    print()

    _print_revocation(provider)
    _print_what_stays()
    return 0


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``uninstall`` verb, per the ``service_unit.add_parser`` convention.

    Args:
        sub: The subparser action every verb is registered on.
    """
    parser = sub.add_parser(
        "uninstall",
        help="Guided offboarding: remove the credential, print the rest.",
        description=(
            "Remove this install's stored credential and print every remaining "
            "offboarding step -- service teardown, provider revocation, and the "
            "local files. Deletes no file and stops no service; filed mail and "
            "labels stay in the mailbox by design."
        ),
    )
    parser.add_argument("provider", choices=["google", "microsoft"])
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt. Required when stdin is not a terminal.",
    )
