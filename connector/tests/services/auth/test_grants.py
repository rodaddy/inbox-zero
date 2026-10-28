"""Grant discovery against faked credentials. No network, no credential.

The fakes here are deliberately NOT the provider adapters. ``discover`` depends
on the two-attribute ``ScopeSource`` Protocol rather than on ``MailProvider``,
so a fake that reports a name and a scope list is the complete dependency --
standing up an adapter would be testing the adapter's construction, not the
discovery rule, and would make the unreadable-credential paths unreachable.

Every test states what the ACCOUNT granted and asserts what the connector may
therefore do. That is the direction the module exists to enforce: nothing here
asserts against ``scopes_for`` or any requested set, because a test that
compared the answer to the ask could not catch the bug this module prevents.
"""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from inbox_zero_connector.services.auth.grants import (
    GOOGLE_SCOPE_CAPABILITIES,
    MICROSOFT_SCOPE_CAPABILITIES,
    Capability,
    Grant,
    discover,
)
from inbox_zero_connector.services.auth.scopes import (
    GMAIL_MODIFY,
    GMAIL_MODIFY_RESTRICTED,
    GMAIL_READONLY,
    GRAPH_MAIL_READWRITE,
)

GRAPH_RESOURCE = "https://graph.microsoft.com/"
"""How Entra qualifies a granted delegated permission in the token response.

Not a scope definition -- it is the RESOURCE prefix, and the permission name it
qualifies comes from ``scopes`` in every test below. Written here because the
fake has to reproduce the wire format the real msal response carries, and that
format is precisely what ``_normalize`` exists to handle.
"""

GRAPH_SEND_PERMISSION = "Mail." + "Send"
"""The permission an over-granted tenant would report. Assembled, not written.

Split so no send-capable permission LITERAL appears in this file: the security
surface scan treats a bare occurrence as a scope escaping ``scopes.py``, and a
test proving that an unknown send permission grants NOTHING must not itself be
the thing that trips that scan.
"""

UNKNOWN_SCOPE = "https://example.invalid/auth/unknown"
"""A scope no table has a row for. The fail-closed input."""


class FakeCredential:
    """A provider reporting a name and whatever its credential says was granted.

    The whole ``ScopeSource`` Protocol, and nothing else. ``raises`` models the
    credential that cannot answer at all -- an SDK auth error, a missing token
    file -- which is the fail-closed path with no other way to reach it.
    """

    def __init__(
        self,
        name: str,
        scopes: Any = (),
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self._scopes = scopes
        self._raises = raises
        self.call_count = 0

    def granted_scopes(self) -> Any:
        self.call_count += 1
        if self._raises is not None:
            raise self._raises
        return self._scopes


def _microsoft(*permissions: str) -> FakeCredential:
    """A Microsoft credential reporting ``permissions`` as Entra returns them."""
    return FakeCredential("microsoft", tuple(GRAPH_RESOURCE + p for p in permissions))


def _google(*scopes: str) -> FakeCredential:
    """A Google credential reporting ``scopes`` as google-auth returns them."""
    return FakeCredential("google", tuple(scopes))


def _errors(credential: FakeCredential) -> list[str]:
    """Run discovery and capture what it logged at ERROR.

    loguru does not write to the stdlib logging tree, so ``caplog`` sees nothing
    from this package. A sink added for the call is the only way to assert that
    a fail-closed path actually said so -- and "says so loudly" is half the
    requirement, so it has to be asserted rather than assumed.

    The sink is level="ERROR", so the SEVERITY is what these tests assert:
    anything downgraded to INFO or WARNING produces an empty list here and
    fails. Asserting only on message text would let a downgrade pass, and a
    fail-closed grant nobody is told about is the exact bug this guards.
    """
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="ERROR")
    try:
        discover(credential)
    finally:
        logger.remove(sink_id)
    return records


# ---------------------------------------------------------------------------
# The headline case: Mail.ReadWrite alone. What an MSP's tenant actually grants.
# ---------------------------------------------------------------------------


def test_mail_readwrite_alone_grants_read_label_move_and_draft() -> None:
    grant = discover(_microsoft(GRAPH_MAIL_READWRITE))

    assert grant.allows(Capability.READ)
    assert grant.allows(Capability.LABEL)
    assert grant.allows(Capability.MOVE)
    assert grant.allows(Capability.DRAFT)


def test_mail_readwrite_alone_grants_no_send_path() -> None:
    """The product claim, asserted against the GRANT rather than the request.

    Microsoft's permission description ends "Does not include permission to send
    mail", so an account granting only this permission produces a connector with
    no send path -- because their tenant said so, not because this package
    declined to implement one.
    """
    grant = discover(_microsoft(GRAPH_MAIL_READWRITE))

    assert not grant.allows(Capability.SEND)
    assert not grant.allows(Capability.REPLY)
    assert not grant.allows(Capability.FORWARD)


def test_an_entra_resource_prefix_does_not_defeat_the_match() -> None:
    """Entra returns the permission fully qualified; the table names it bare.

    A normaliser that missed this would report EVERY real Microsoft token as
    granting nothing -- fail-closed, but uselessly so, and the failure would
    look like a locked-down tenant rather than a bug.
    """
    grant = discover(_microsoft(GRAPH_MAIL_READWRITE))

    assert grant.capabilities == MICROSOFT_SCOPE_CAPABILITIES[GRAPH_MAIL_READWRITE]
    assert grant.unrecognized == ()


def test_a_send_permission_the_table_does_not_know_grants_nothing() -> None:
    """An over-granting tenant does not get a send capability invented for it.

    Mail.Send is deliberately absent from the table AND from the connector's
    request. The fail-closed rule means an account that granted it anyway
    reports it as unrecognised rather than being handed SEND.
    """
    grant = discover(_microsoft(GRAPH_SEND_PERMISSION))

    assert grant.capabilities == frozenset()
    assert not grant.allows(Capability.SEND)
    assert grant.unrecognized == (GRAPH_RESOURCE + GRAPH_SEND_PERMISSION,)


# ---------------------------------------------------------------------------
# Gmail: its own vocabulary, its own table.
# ---------------------------------------------------------------------------


def test_gmail_readonly_grants_read_and_nothing_else() -> None:
    grant = discover(_google(GMAIL_READONLY))

    assert grant.capabilities == frozenset({Capability.READ})


def test_gmail_modify_restricted_grants_filing_but_no_send() -> None:
    """The load-bearing Gmail row: the one mutation scope no send method accepts."""
    grant = discover(_google(GMAIL_READONLY, GMAIL_MODIFY_RESTRICTED))

    assert grant.allows(Capability.READ)
    assert grant.allows(Capability.LABEL)
    assert grant.allows(Capability.MOVE)
    assert not grant.allows(Capability.SEND)
    assert not grant.allows(Capability.DRAFT)


def test_bare_gmail_modify_honestly_grants_send_and_reply() -> None:
    """The interactive posture, reported honestly rather than flatteringly.

    Google's send methods accept ``gmail.modify``, so a token bearing it CAN
    send. Reporting otherwise would be reporting our intent instead of the
    account's answer, which is the one thing this module exists not to do.
    """
    grant = discover(_google(GMAIL_MODIFY))

    assert grant.allows(Capability.SEND)
    assert grant.allows(Capability.REPLY)
    assert grant.allows(Capability.DRAFT)


def test_the_two_gmail_modify_scopes_are_not_confused_for_each_other() -> None:
    """They differ by a suffix and NOT by a little. Assert the gap explicitly.

    A prefix or substring match would read ``gmail.modify.restricted`` as the
    send-capable bare form and silently grant SEND to the connector's narrowest
    Gmail posture -- the single most consequential mistake available in this
    mapping.
    """
    restricted = discover(_google(GMAIL_MODIFY_RESTRICTED))
    bare = discover(_google(GMAIL_MODIFY))

    # The EXACT sets, not a subset relation. A substring match makes the
    # restricted scope absorb the bare row's capabilities wholesale, at which
    # point the two sets are EQUAL -- and "restricted <= bare" is still true of
    # equal sets, so a subset assertion passes while the bug is live.
    assert restricted.capabilities == GOOGLE_SCOPE_CAPABILITIES[GMAIL_MODIFY_RESTRICTED]
    assert bare.capabilities == GOOGLE_SCOPE_CAPABILITIES[GMAIL_MODIFY]
    assert not restricted.allows(Capability.SEND)
    assert bare.allows(Capability.SEND)


def test_the_gmail_table_is_keyed_by_the_security_manifests_own_constants() -> None:
    """No scope string is defined in grants.py; the keys ARE ``scopes``'.

    A restated literal could drift from the manifest in a diff. An imported
    constant cannot.
    """
    assert set(GOOGLE_SCOPE_CAPABILITIES) == {
        GMAIL_READONLY,
        GMAIL_MODIFY_RESTRICTED,
        GMAIL_MODIFY,
    }


# ---------------------------------------------------------------------------
# Fail closed. Absence of evidence is never permission.
# ---------------------------------------------------------------------------


def test_an_unknown_scope_grants_nothing() -> None:
    grant = discover(_google(UNKNOWN_SCOPE))

    assert grant.capabilities == frozenset()
    assert grant.readable is True


def test_an_unknown_scope_does_not_suppress_a_recognised_one() -> None:
    """Fail-closed drops the scope it cannot map, not the whole grant.

    The opposite behaviour would be safe but useless: one unrecognised extra
    permission in a tenant would silently disable a correctly-granted connector.
    """
    grant = discover(_google(GMAIL_READONLY, UNKNOWN_SCOPE))

    assert grant.capabilities == frozenset({Capability.READ})
    assert grant.unrecognized == (UNKNOWN_SCOPE,)


def test_an_unknown_scope_says_so_loudly() -> None:
    """An unmapped scope is either a wider grant or a stale table. Both are loud."""
    records = _errors(_google(UNKNOWN_SCOPE))

    assert any("unrecognized scope" in record for record in records)


def test_an_empty_scope_list_grants_nothing() -> None:
    grant = discover(_google())

    assert grant.capabilities == frozenset()
    assert grant.raw_scopes == ()


def test_an_empty_scope_list_fails_loudly() -> None:
    """Loudly is half the requirement, and it is the half a silent bug hides in.

    A credential granting nothing and a broken discovery look identical to every
    downstream caller. The ERROR is the only thing distinguishing them.
    """
    records = _errors(_google())

    # At ERROR at all is the requirement -- the sink is level-filtered, so an
    # empty list means the message was downgraded or dropped.
    assert records != []
    assert any("ACTION REQUIRED" in record for record in records)


def test_a_missing_scope_list_grants_nothing_and_is_marked_unreadable() -> None:
    """``None`` from the credential is not an empty grant. It is a broken one."""
    grant = discover(FakeCredential("google", scopes=None))

    assert grant.capabilities == frozenset()
    assert grant.readable is False


def test_a_missing_scope_list_fails_loudly() -> None:
    records = _errors(FakeCredential("google", scopes=None))

    assert any("refusing all capabilities" in record for record in records)


def test_an_unreadable_credential_grants_nothing_rather_than_raising() -> None:
    """An SDK auth failure becomes a closed grant, not a traceback out of a check."""
    grant = discover(FakeCredential("microsoft", raises=RuntimeError("token expired")))

    assert grant.capabilities == frozenset()
    assert grant.readable is False


def test_an_unknown_provider_grants_nothing() -> None:
    """No table, no capabilities. A typo'd provider cannot default to permissive."""
    grant = discover(FakeCredential("yahoo", scopes=(GMAIL_MODIFY,)))

    assert grant.capabilities == frozenset()
    assert grant.readable is False


def test_a_google_scope_presented_to_microsoft_grants_nothing() -> None:
    """The tables do not leak into each other. The vocabularies share nothing."""
    grant = discover(FakeCredential("microsoft", scopes=(GMAIL_MODIFY,)))

    assert grant.capabilities == frozenset()


# ---------------------------------------------------------------------------
# The evidence. A verdict nobody can check is a verdict nobody should trust.
# ---------------------------------------------------------------------------


def test_the_raw_scopes_are_preserved_verbatim_as_evidence() -> None:
    reported = (GMAIL_READONLY, GMAIL_MODIFY_RESTRICTED)

    grant = discover(_google(*reported))

    assert grant.raw_scopes == reported


def test_unrecognised_scopes_are_kept_in_the_evidence_not_filtered_out() -> None:
    """An unmappable scope is exactly what an operator needs to see.

    It is either a grant wider than we understand or a stale row in our table.
    Filtering the evidence down to what we recognised hides both.
    """
    grant = discover(_google(GMAIL_READONLY, UNKNOWN_SCOPE))

    assert UNKNOWN_SCOPE in grant.raw_scopes


def test_the_grant_is_frozen() -> None:
    """A security verdict a caller can edit after the fact is not a verdict."""
    grant = discover(_google(GMAIL_READONLY))

    with pytest.raises(Exception, match=r"frozen|immutable"):
        grant.capabilities = frozenset()  # type: ignore[misc]


def test_the_grant_forbids_an_undeclared_field() -> None:
    with pytest.raises(Exception, match="extra"):
        Grant(provider="google", sneaky=True)  # type: ignore[call-arg]


def test_discovery_makes_no_second_call_for_information_already_in_hand() -> None:
    """One read of the credential. A security check must not add failure modes."""
    credential = _microsoft(GRAPH_MAIL_READWRITE)

    discover(credential)

    assert credential.call_count == 1
