"""The ``setup`` subcommand: the interactive first-install wizard.

``init-config`` is the flag-driven writer an installing agent or a script
calls; this is the same write reached by ASKING a human at a terminal, so a
buyer who was handed the kit is never required to compose a six-flag command
line correctly on their first day.

Two properties make this a security surface rather than a convenience, and
both are enforced here rather than documented elsewhere:

* **It refuses without a TTY.** The gate is checked FIRST, before any prompt
  and before anything is written. A host AI reaching this verb through an MCP
  tool call would be the model configuring the permissions it is itself
  subject to -- the same posture ``auth`` takes -- so a non-terminal caller
  gets a refusal and an untouched disk.
* **It never prompts for a credential VALUE.** The wizard asks for OAuth
  CLIENT IDENTIFIERS, which are not secrets: they identify the buyer's own app
  registration, and ``config_writer.IDENTIFIER_FIELDS`` is the allowlist that
  lets them into the config file. ``google_client_secret`` and
  ``classifier_api_key`` are never asked for at all. Instead the wizard creates
  the empty skeleton at ``secrets_path()`` and points the human at it, so no
  credential ever passes through a terminal, a scrollback buffer, or an
  agent's context window.

The writing itself is delegated to ``write_config``, with the SAME constructor
calls ``_cmd_init_config`` makes. A wizard that assembled its own payload would
be a second writer to keep in step with the first; there is one writer, and this
module only decides what to hand it.

Lives outside ``main.py`` for the same reason ``desktop.py`` does: that module
carries the parser and dispatch table for every verb and sits against the repo's
code-line ceiling, and this verb is a self-contained interactive flow with no
shared state.
"""

from __future__ import annotations

import sys

# Straight from `accounts`, not through the `config` keystone. The keystone is
# AT its 500-code-line ceiling, so it carries the `AccountsMixin` it needs for
# the field and re-exports nothing else from this module.
from inbox_zero_connector.accounts import AccountSettings, infer_provider
from inbox_zero_connector.config import (
    DEFAULT_VOICES,
    ClassifierSettings,
    ConnectorConfig,
    Posture,
    VoiceRouting,
    config_path,
    write_config,
    write_secrets_skeleton,
)

# From `config_writer` rather than the `config` keystone: the keystone sits one
# line under its 500-code-line ceiling, so a third re-export block would breach
# it. `init_config.py` imports it the same way, for the same reason.
from inbox_zero_connector.config_writer import ensure_install_id
from inbox_zero_connector.services.auth import scopes as scopes

PROVIDERS = scopes.PROVIDERS


def _ask(prompt: str, default: str | None = None) -> str:
    """Ask one question, showing its default, and hand back the stripped answer.

    Wraps ``input`` in ONE place so the whole wizard is scriptable from a test
    by monkeypatching ``builtins.input`` -- and so the default is rendered the
    same way every time rather than by eleven hand-written prompt strings.

    Args:
        prompt: The question, without punctuation or the default marker.
        default: Shown in brackets and returned when the human presses enter.

    Returns:
        The answer with surrounding whitespace removed, or ``default`` (or
        ``""`` when there is none) for an empty response.
    """
    suffix = f" [{default}]" if default is not None else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    if answer:
        return answer
    return default or ""


def _ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    """Ask a yes/no question. Anything but an explicit yes is a no.

    The asymmetry is deliberate everywhere this is used: every yes/no in this
    wizard turns a capability ON, so an ambiguous answer must leave it off.
    """
    marker = "Y/n" if default else "y/N"
    answer = _ask(f"{prompt} [{marker}]").lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _drafts_from_accounts(accounts: tuple[AccountSettings, ...]) -> dict[str, bool]:
    """Fold the per-account drafts flags into the install-level per-PROVIDER dict.

    ``ConnectorConfig.drafts_enabled`` is keyed by provider, not by mailbox, and
    it is what decides the OAuth scope this install requests -- one consent per
    provider, not one per mailbox. So the fold has to be an OR: a provider is
    asked for the drafting scope when ANY account on it wants drafts, or the
    account that wants them would silently get none.

    The per-account ``drafts`` flag is not thereby redundant. It remains the
    capability switch the tool layer honours for that mailbox (D11), which is
    what lets one Gmail account draft while another files only -- the scope is
    shared, the permission is not.

    Args:
        accounts: The collected list, at least one entry.

    Returns:
        Every provider name the connector knows, mapped to whether any account
        on it asked for drafts. Absent providers are False rather than missing,
        which is the shape ``ConnectorConfig`` fills in anyway.
    """
    folded = dict.fromkeys(PROVIDERS, False)
    for account in accounts:
        if account.drafts:
            folded[account.provider] = True
    return folded


def _ask_provider(address: str) -> str:
    """The provider for one mailbox: inferred from the address, or asked.

    The inference is ANNOUNCED rather than silent. A human who typed a Gmail
    address should see that the wizard read it correctly, because the one thing
    they cannot recover from later is an account pointed at the wrong consent
    screen -- that fails at sign-in with a message about the provider, not about
    the address that chose it.

    Args:
        address: The mailbox address just collected.

    Returns:
        ``"google"`` or ``"microsoft"``. Asked, with no default, when the domain
        does not state it -- a custom domain is served by either and guessing
        would pick the wrong one silently.
    """
    inferred = infer_provider(address)
    if inferred is not None:
        print(f"  provider: {inferred} (from the address domain)")
        return inferred
    answer = _ask("  Provider for this mailbox -- google or microsoft").lower()
    while answer not in {"google", "microsoft"}:
        print("  Not a provider. Type google or microsoft.")
        answer = _ask("  Provider for this mailbox -- google or microsoft").lower()
    return answer


def _ask_one_account(index: int) -> AccountSettings:
    """Collect one mailbox: address, provider, owner domain, posture, drafts.

    Posture and the drafts flag are asked PER ACCOUNT (D11) because they are the
    security answer for one mailbox, and a firm routinely has a different answer
    for a personal mailbox than for a client-facing one. The taxonomy is NOT
    asked here: it is shared across every account on the install.

    Args:
        index: 1-based position, shown so the human can see where they are.

    Returns:
        The validated account. Construction is what normalises and refuses, so
        a bad answer fails here with the field named rather than at load.
    """
    print()
    print(f"Account {index}:")
    address = _ask("  Mailbox address")
    while not address:
        print("  A mailbox address is required.")
        address = _ask("  Mailbox address")
    provider = _ask_provider(address)
    owner_domain = _ask("  Owner mail domain for this mailbox", default=address.rpartition("@")[2])
    posture = _ask("  Posture -- file_only or file_read_draft", default=Posture.FILE_ONLY.value)
    while posture not in {member.value for member in Posture}:
        print(
            f"  Not a posture. Choose {Posture.FILE_ONLY.value} or {Posture.FILE_READ_DRAFT.value}."
        )
        posture = _ask("  Posture -- file_only or file_read_draft", default=Posture.FILE_ONLY.value)
    drafts = _ask_yes_no("  Enable reply drafts on this mailbox?")
    return AccountSettings(
        address=address,
        provider=provider,
        owner_domain=owner_domain,
        posture=posture,
        drafts=drafts,
    )


def _ask_accounts() -> tuple[AccountSettings, ...]:
    """Collect the WHOLE account list before any auth begins.

    THE ORDER IS THE POINT. Every question in this pass is answerable from
    memory at a keyboard; every question after it opens a browser and waits on a
    consent screen. Asking them together means a human with three mailboxes
    answers three short blocks once, instead of finding out after the first
    sign-in that the installer wants running again. The auth pass then walks
    this list in one go.

    Returns:
        At least one account, in the order given. The FIRST is the install's
        default -- every existing single-account caller keeps reading the
        install-level fields, which describe exactly that account.
    """
    print()
    print("Mailboxes. List every account this install is being set up for; the")
    print("FIRST one is this install's default. Nothing signs in until the whole")
    print("list is collected, so you can answer all of this from memory.")
    print()
    print("Posture only NARROWS -- file_only composes NO drafts whatever the")
    print("drafts answer says; file_read_draft permits drafting where the drafts")
    print("answer ALSO allows it. Neither marks mail read.")
    print(f"Drafts cost no extra scope on either provider: {scopes.GMAIL_MODIFY}")
    print(f"and {scopes.GRAPH_MAIL_READWRITE} already cover draft creation, so the")
    print("answer is the capability switch the tool layer honours, not a new consent.")
    accounts: list[AccountSettings] = []
    while True:
        accounts.append(_ask_one_account(len(accounts) + 1))
        if not _ask_yes_no("Add another mailbox?"):
            return tuple(accounts)


def _ask_sales_labels() -> tuple[str, ...]:
    """Ask for the thread labels that route to the sales voice. Empty is fine."""
    print()
    answer = _ask("Thread labels routing to the sales voice, comma-separated (blank for none)")
    return tuple(part.strip() for part in answer.split(",") if part.strip())


def _ask_classifier() -> ClassifierSettings:
    """Ask the vendor switch: where classification goes, and what runs there.

    The base URL is named as the EGRESS DESTINATION at the prompt. It is the
    single field in this wizard that decides whether message text leaves the
    machine, and calling it "base_url" without saying so would make that a
    detail rather than a decision.
    """
    defaults = ClassifierSettings()
    print()
    print("Classifier. The base URL is THE EGRESS DESTINATION: subject lines and")
    print("body excerpts are sent there for classification. Leave it blank to keep")
    print("the built-in default and send nothing to a third party you did not name.")
    raw_base_url = _ask("Classifier base URL (blank for the default)")
    model = _ask("Classifier model", default=defaults.model)
    requires_key = _ask_yes_no("Does that endpoint require an API key?")
    return ClassifierSettings(
        base_url=raw_base_url or None,
        model=model,
        requires_key=requires_key,
    )


def _ask_identifiers() -> dict[str, str]:
    """Ask the OAuth CLIENT IDENTIFIERS -- which are not secrets, and are asked plainly.

    These name the buyer's own app registration the way a vault ITEM NAME names
    a credential without being one. They are written into
    ``connector-config.json``, a file the installer rewrites and a support
    request may quote, which is exactly why the VALUES that pair with them are
    never asked for here.

    Returns:
        Only the answers the human actually gave. An empty answer is omitted
        rather than written as ``""``, so a wizard pass that skips a provider
        does not blank an identifier a previous pass established.
    """
    print()
    print("OAuth client identifiers. These are NOT credential values -- they name")
    print("an app registration, and they belong in the config file.")
    print("Microsoft: leave blank to use rTech's shipped multi-tenant public client;")
    print("fill it in only to point at your own Entra registration.")
    answers = {
        "google_client_id": _ask("Google OAuth client ID (blank to skip)"),
        "microsoft_client_id": _ask("Microsoft application (client) ID (blank to skip)"),
        "microsoft_tenant_id": _ask("Microsoft tenant ID", default="common"),
    }
    return {key: value for key, value in answers.items() if value}


def _point_at_the_secrets_file() -> None:
    """Create the empty skeleton and tell the human it is THEIRS to fill in.

    This is the whole reason no prompt in this module says the word "secret":
    the two credential VALUES have a home that is not a terminal, and the
    wizard's job is to create that home with the right shape and permissions
    and then get out of the way.
    """
    path, created = write_secrets_skeleton()
    print()
    print(f"Credential values go in {path}")
    print(f"  ({'created empty just now' if created else 'already existed; left untouched'})")
    print("  You fill in google_client_secret and classifier_api_key THERE, in your")
    print("  own editor. This wizard never asks for them and nothing in this")
    print("  connector ever writes a real credential to that file.")
    print("  Environment variables still work, and they WIN over the file.")


def _print_summary(config: ConnectorConfig, routing: VoiceRouting, install_id: str) -> None:
    """Print the same per-provider drafts/no-send report ``init-config`` ends with.

    Args:
        config: The posture and per-provider drafts flags just written.
        routing: The voice routing table just written.
        install_id: The id now naming this install. Printed because every path
            on the line above is namespaced by it, and because a human who just
            ran the wizard is the one person who can still notice that the id
            is not the one they expected.
    """
    print()
    print(f"Wrote connector config to {config_path()}.\n")
    print(f"install id   : {install_id}")
    print(f"posture      : {config.posture.value}")
    print(f"owner domain : {routing.owner_domain}")
    print(f"voices       : {', '.join(routing.voices)}")
    print(f"sales labels : {', '.join(routing.sales_labels) or '(none)'}")
    print()
    for name in PROVIDERS:
        drafts_on = config.drafts_on(name)
        print(f"{name}: drafts={drafts_on}, no-send={scopes.enforcement_for(name, drafts_on)}")


def _refuse_without_a_tty() -> bool:
    """True when there is no human terminal on both ends of this process.

    Read at CALL time, never cached at import: the check has to describe the
    process actually running the command, and a module-level flag would be
    answering about whichever process happened to import the module first.
    """
    return not (sys.stdin.isatty() and sys.stdout.isatty())


def cmd_setup() -> int:
    """Run the wizard: gate, confirm, ask, write, and point at the secrets file.

    The install id is minted BEFORE the overwrite check (#171), for the reason
    ``cmd_init_config`` states: the id is the last segment of the config
    directory, so a path resolved ahead of the mint would be tested for
    existence and then not be the path written. The mint is also the only thing
    standing between a fresh unzip and a keychain that refuses to build an
    account name for it -- the dead end the #171 refusal left behind, and the
    reason that refusal's message names this command.

    Returns:
        ``0`` after the config is written, ``1`` when the TTY gate refuses or an
        existing config is left in place. Both non-zero paths write nothing
        except the minted id, which is the install's identity rather than a
        setting and must survive an abandoned wizard run: re-minting on the
        next attempt would hand the install a different config directory, data
        directory, and keychain account than the one it just refused to
        overwrite.
    """
    if _refuse_without_a_tty():
        print(
            "setup is an INTERACTIVE install step for a human at a terminal, and it "
            "never runs under a host AI or an MCP tool call -- the same posture auth "
            "takes. Nothing was written. Run it yourself in a terminal, or use "
            "init-config with flags.",
            file=sys.stderr,
        )
        return 1

    # AFTER the TTY gate, which must leave the disk untouched, and BEFORE
    # `config_path()`, which resolves through the id this call establishes.
    resolved_install_id = ensure_install_id()

    target = config_path()
    if target.exists() and not _ask_yes_no(f"Config already exists at {target}. Overwrite?"):
        print("Nothing written. The existing config is unchanged.")
        return 1

    accounts = _ask_accounts()
    sales_labels = _ask_sales_labels()
    classifier = _ask_classifier()
    identifiers = _ask_identifiers()

    # The FIRST account is the install's default, and these three install-level
    # fields describe it. That is what keeps every existing single-account
    # caller -- the watch loop, `auth`, `show-scopes`, the MCP server -- working
    # unchanged against a config the wizard now writes an account LIST into.
    default = accounts[0]
    routing = VoiceRouting(
        owner_domain=default.owner_domain,
        voices=DEFAULT_VOICES,
        sales_labels=sales_labels,
    )
    config = ConnectorConfig(
        drafts_enabled=_drafts_from_accounts(accounts),
        posture=Posture(default.posture),
        routing=routing,
    )
    write_config(config, identifiers=identifiers, classifier=classifier, accounts=accounts)

    _point_at_the_secrets_file()
    _print_summary(config, routing, resolved_install_id)
    return 0
