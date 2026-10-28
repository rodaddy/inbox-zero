"""One round covers EVERY configured account, each with its own drafts answer (#69).

The defect this pins is the shape the watcher had before decision D11 landed in
the loop: an install could RECORD three mailboxes and watch exactly one, so the
second and third accumulated mail forever while every log line and every health
check read clean. The two tests below are the two halves of what "the watcher
runs every account" has to mean -- each account gets a pass in the SAME round,
and each account's pass carries THAT account's posture rather than the first
one's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from inbox_zero_connector.accounts import AccountSettings
from inbox_zero_connector.apps.cli import watch_accounts
from inbox_zero_connector.config import ConfigError, Settings
from inbox_zero_connector.services.watch.accounts_pass import AccountRound, run_accounts


def _account(address: str, *, provider: str, posture: str) -> AccountSettings:
    """One configured account. ``drafts`` follows the posture, as a real one does."""
    return AccountSettings(
        address=address,
        provider=provider,
        owner_domain=address.split("@", maxsplit=1)[1],
        posture=posture,
        drafts=posture == "file_read_draft",
    )


def _settings_with(
    *, accounts: tuple[AccountSettings, ...], drafts_enabled: dict[str, bool]
) -> Settings:
    """A settings object carrying just the fields the assembly reads.

    ``posture`` is the INSTALL-level one and is set permissive here on purpose:
    these tests are about the PER-ACCOUNT narrowing, so the install-level gate
    must not be what produces the False -- otherwise the assertion would pass
    against an implementation that ignored the account entirely.
    """
    return Settings(
        accounts=accounts,
        drafts_enabled=drafts_enabled,
        posture="file_read_draft",
    )


class _Recorder:
    """A stand-in for ``loop.run_pass`` that records which context it was given."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.drafts: list[bool] = []

    def __call__(self, context: object, _checkpoint: Callable[[], object] | None) -> str:
        account = getattr(context, "account", "")
        self.seen.append(account)
        # The drafts answer reaches a round through the PROVIDER it was built
        # with, which is the scope decision, so that is where the per-account
        # flag has to be visible. Reading it off the provider rather than off a
        # separate field is deliberate: a test that asserted a config value
        # would pass while the provider was still built from the first
        # account's answer, which is the exact defect.
        self.drafts.append(bool(getattr(context.provider, "drafts_enabled", False)))
        return f"pass:{account}"


@dataclass(frozen=True)
class _FakeProvider:
    """Carries only the one attribute this test reads."""

    drafts_enabled: bool


class _FakeContext:
    """A context stand-in: the account it serves and the provider it was built with."""

    def __init__(self, account: str, *, drafts_enabled: bool) -> None:
        self.account = account
        self.provider = _FakeProvider(drafts_enabled=drafts_enabled)


def test_two_accounts_with_different_drafts_flags_both_file_in_one_round() -> None:
    """Both accounts get a pass, in order, each keeping its OWN drafts answer.

    THE DIFFERENT FLAGS ARE THE POINT, not decoration. Two accounts that agreed
    on drafts would pass against an implementation that built one provider from
    the first account and reused it for both -- which is precisely the bug D11
    exists to prevent, because it silently gives a file-only mailbox a
    send-capable scope.
    """
    run_pass = _Recorder()
    logged: list[tuple[str, str]] = []
    rounds = (
        AccountRound(
            address="first@example.com",
            context=_FakeContext("first@example.com", drafts_enabled=True),  # type: ignore[arg-type]
        ),
        AccountRound(
            address="second@example.com",
            context=_FakeContext("second@example.com", drafts_enabled=False),  # type: ignore[arg-type]
        ),
    )

    results = run_accounts(
        rounds, run_pass, lambda result, address: logged.append((address, result))
    )

    assert run_pass.seen == ["first@example.com", "second@example.com"]
    assert run_pass.drafts == [True, False]
    assert results == ["pass:first@example.com", "pass:second@example.com"]


def test_the_assembly_gives_each_account_its_own_posture_and_drafts_answer() -> None:
    """The CLI's own assembly, not a stub: per-account posture reaches the scope.

    THE OTHER HALF OF THE SAME DEFECT. The test above proves the fan-out honours
    whatever contexts it is given; this one proves the contexts are BUILT per
    account -- a file-only mailbox beside a drafting one on the same install
    must not inherit the drafting one's scope. It drives
    ``watch_accounts.build_rounds``, which is where the three-way AND
    (account drafts flag, account posture, install scope flag) actually lives.
    """
    specs = watch_accounts._account_specs(
        _settings_with(
            accounts=(
                _account("draft@example.com", provider="google", posture="file_read_draft"),
                _account("fileonly@example.com", provider="microsoft", posture="file_only"),
            ),
            drafts_enabled={"google": True, "microsoft": True},
        )
    )

    assert [(address, drafts) for address, _provider, drafts in specs] == [
        ("draft@example.com", True),
        # DRAFTS OFF despite the install's microsoft flag being ON, because THIS
        # account's posture is file_only. The narrower answer wins per account.
        ("fileonly@example.com", False),
    ]


def test_two_accounts_on_one_provider_are_refused_rather_than_sharing_a_credential() -> None:
    """One keychain account per provider cannot serve two mailboxes (#55 layout).

    Both would resolve the same stored token and poll whichever mailbox signed
    in last -- one watched twice, the other not at all, with receipts naming
    both. Refusing names the fix; running would produce a wrong audit trail.
    """
    with pytest.raises(ConfigError, match="two google mailboxes"):
        watch_accounts._account_specs(
            _settings_with(
                accounts=(
                    _account("one@example.com", provider="google", posture="file_only"),
                    _account("two@example.com", provider="google", posture="file_only"),
                ),
                drafts_enabled={"google": False, "microsoft": False},
            )
        )
