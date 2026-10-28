"""Turn the installed config into the account rounds ``watch`` polls (#69).

WHAT THIS MODULE IS
    The assembly step between the settings keystone and ``loop.run``. It reads
    the configured account list, builds ONE fully-resolved ``RoundContext`` per
    account -- that account's provider, that account's scope, that account's
    drafts answer -- and hands back the list plus the single checkpoint that
    persists every account's stream position.

    It is a module of its own rather than more of ``main._cmd_watch`` for the
    reason ``watch.py`` next door is one: that function is already at the
    per-function code-line ceiling (``scripts/check_code_size.py``) and its
    docstring is the ``--once`` exit contract, which must not be shortened to
    make room for assembly code.

WHAT IS PER ACCOUNT AND WHAT IS SHARED IS DECISION D11, NOT A GUESS
    Shared: the TAXONOMY. It is loaded once and the same object is placed on
    every context, because the folder vocabulary is the one the owner agreed to
    and two of their mailboxes filing into two vocabularies is a support call.
    Per account: the POSTURE and the DRAFTS flag, because they are the security
    answer for one mailbox -- a personal Gmail that may draft beside a
    client-facing mailbox that files only is the ordinary case, and one shared
    answer forces one of the two to be wrong in the WIDENING direction.

THE FALLBACK IS THE HISTORICAL SHAPE AND IT IS EXACT
    An install whose config lists no accounts -- every install written before
    the field existed -- yields exactly one round built from the install-level
    ``provider``, ``posture``, and ``drafts_enabled``, with an EMPTY address.
    That is the same context ``_cmd_watch`` built before this module existed,
    so its log line, its receipts, and its state file are unchanged. Empty
    rather than a synthesised address: nothing here knows the mailbox's name,
    and inventing one would stamp a guess onto every receipt.

WHAT IT REFUSES TO DO: SHARE A CREDENTIAL ACROSS TWO ACCOUNTS
    The OS keychain account is keyed by install id plus PROVIDER
    (``services/auth/keychain.py``), and #55 settled that this layout does not
    change. So two configured accounts on the SAME provider resolve the same
    stored token and would both poll whichever mailbox last authenticated --
    one mailbox watched twice, the other not at all, with receipts naming both.
    That is worse than not running, so it is refused at assembly with the fix
    named. Accounts on DIFFERENT providers have different keychain accounts and
    different state files, and those run together correctly.
"""

from __future__ import annotations

from collections.abc import Callable

from inbox_zero_connector.classifiers import factory
from inbox_zero_connector.config import ConfigError, Settings
from inbox_zero_connector.providers import get_provider
from inbox_zero_connector.services.triage.taxonomy import load_taxonomy
from inbox_zero_connector.services.watch.accounts_pass import AccountRound
from inbox_zero_connector.services.watch.round import DraftComposer, RoundContext
from inbox_zero_connector.services.watch.state import WatchState


def build_rounds(
    active: Settings, *, dry_run: bool = False
) -> tuple[tuple[AccountRound, ...], Callable[[], None]]:
    """Build one round per configured account, plus the checkpoint that saves them.

    Args:
        active: The loaded settings keystone.
        dry_run: Passed to every context unchanged. A preview previews every
            account, because an operator checking an install before its first
            real pass needs to see all of the folders it is about to create.

    Returns:
        The account rounds in configured order (first account first), and a
        single callable that persists EVERY account's watch state. One
        checkpoint rather than one per account because ``loop.run_pass`` calls
        it after each stream and knows nothing about accounts; saving all of
        them is cheap, and saving only the current one would need the loop to
        thread an account through a signature that exists for persistence.

    Raises:
        ConfigError: Two configured accounts share one provider. See the module
            docstring -- one keychain account cannot serve two mailboxes.
    """
    taxonomy = load_taxonomy()
    classifier = factory.from_settings(active)
    connector_config = active.connector_config()
    states: dict[str, WatchState] = {}
    rounds: list[AccountRound] = []
    for address, provider_name, drafts in _account_specs(active):
        state = states.setdefault(provider_name, WatchState.load(provider_name))
        rounds.append(
            AccountRound(
                address=address,
                context=RoundContext(
                    provider=get_provider(provider_name, drafts_enabled=drafts),
                    scope=connector_config.mailbox_scope(provider_name),
                    taxonomy=taxonomy,
                    state=state,
                    classifier=classifier,
                    routing=connector_config.routing if drafts else None,
                    # BOTH HALVES OR NEITHER (#76). ``round._draft`` refuses to
                    # compose unless the backend AND the routing are present, so
                    # a wiring step that supplies one of them produces a watcher
                    # whose drafting limb can never run and says nothing about
                    # why. The composer is the classification classifier itself:
                    # ``drafting.DraftBodyBackend`` states that the composing
                    # model IS the classification model, one configuration.
                    #
                    # ``isinstance`` rather than a bare ``classifier``, because
                    # ``factory.from_settings`` returns ``... | None`` -- a
                    # rule-only install has no model backend at all, and
                    # ``DraftComposer`` is ``runtime_checkable``, so the one
                    # expression answers both "is there a backend" and "can it
                    # compose". A rule-only install with drafts on therefore
                    # gets ``None`` here and files without drafting, rather than
                    # failing at the first message on a missing method.
                    draft_backend=(
                        classifier if drafts and isinstance(classifier, DraftComposer) else None
                    ),
                    dry_run=dry_run,
                    account=address,
                ),
            )
        )

    def checkpoint() -> None:
        """Persist every provider's watch state once, covering all accounts.

        Returned to the caller rather than run here because the states are only
        safe to save after a round completes, and the loop owns that moment. A
        single closure over ``states`` means the caller cannot forget a provider
        or save them at different points within one pass.
        """
        for provider_name, state in states.items():
            state.save(provider_name)

    return tuple(rounds), checkpoint


def _account_specs(active: Settings) -> list[tuple[str, str, bool]]:
    """The (address, provider, drafts) triples this install watches.

    THE DRAFTS ANSWER IS AN AND OF THREE, AND EVERY TERM ONLY NARROWS. The
    account's own ``drafts`` flag, the account's own posture, and the install's
    provider scope flag must all say yes. Two of those are per account, which
    is D11; the third is the install-level audited config, and it is in the
    conjunction so that a per-account toggle can never request a send-capable
    Gmail scope the audited file never asked for. Widening from a per-account
    field is the failure this shape exists to make impossible -- a new account
    can turn drafts OFF for its mailbox, never ON for the install.

    Raises:
        ConfigError: Two accounts name the same provider.
    """
    if not active.accounts:
        # The historical shape, BYTE FOR BYTE. ``drafts_on`` rather than
        # ``drafts_allowed`` because that is what `_cmd_watch` passed to
        # ``get_provider`` before this module existed, and this flag decides
        # which SCOPE is requested -- narrowing it here would change which
        # consent an existing install asks Google for, which is a behaviour
        # change wearing a safety argument. The posture gate is not skipped by
        # this: `drafts_allowed` is still what decides whether a draft is
        # COMPOSED (`round.py`), and this flag only decides which scope is
        # requested -- the same split `ConnectorConfig.drafts_allowed`
        # documents. The empty address says honestly that nothing recorded
        # which mailbox.
        return [("", active.provider, active.connector_config().drafts_on(active.provider))]
    seen: dict[str, str] = {}
    specs: list[tuple[str, str, bool]] = []
    connector_config = active.connector_config()
    for account in active.accounts:
        if account.provider in seen:
            raise ConfigError(
                f"accounts lists two {account.provider} mailboxes "
                f"({seen[account.provider]} and {account.address}), and this "
                "install stores one credential per provider -- both would poll "
                "whichever mailbox signed in last. ACTION REQUIRED: give the "
                "second mailbox its own install with its own install_id "
                "(INSTALL-RUNBOOK.md 6b)."
            )
        seen[account.provider] = account.address
        drafts = (
            account.drafts
            and account.posture == "file_read_draft"
            and connector_config.drafts_on(account.provider)
        )
        specs.append((account.address, account.provider, drafts))
    return specs
