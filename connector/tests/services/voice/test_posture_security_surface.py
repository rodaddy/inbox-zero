"""THE CLIENT POSTURE GATE -- posture NARROWS the drafts flag, never widens it.

These are SECURITY assertions, not drafting-behaviour assertions, and they were
written to sit in ``tests/test_security_surface.py`` for that reason: the
property under test is not "does the folder table work", it is "can a
client-level toggle reach a scope decision", and the answer has to be no in the
file an auditor reads. A regression here is a security regression.

WHY THEY LIVE HERE INSTEAD, AND WHY THAT IS NOT A DOWNGRADE (#146). They test
the voice add-on's ``drafting`` module, so they must import it -- and
``tests/test_security_surface.py`` ships in the BASIC zip, which does not
contain that module. Keeping them there made the basic build fail the packaging
judge and, had it shipped, made the file uncollectable on the tier it shipped
to. ``tests/services/voice/**`` is excluded from the basic tier alongside the
module itself (``scripts/packaging/tiers.json``), so here the tests ship exactly
where their subject ships and run wherever drafting is installable at all.

The gate they guard only exists on the tier that can draft, so the tier that
can draft is the tier that must assert it. Nothing was dropped in the move:
every assertion below is byte-for-byte the one that ran before it.

Its sibling, ``test_posture_never_reaches_the_scope_module``, deliberately did
NOT move -- it asserts against ``services/auth/scopes``, which every tier
ships, and it names no add-on module.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.config import ConnectorConfig, Posture
from inbox_zero_connector.services.voice import drafting as drafting
from inbox_zero_connector.services.voice.drafting import POSTURE_REFUSAL, ThreadContext, may_draft


def _drafting_context() -> object:
    """A thread that WOULD draft, so a refusal can only be the posture."""
    return ThreadContext(
        sender="client@customer.example",
        participants=("client@customer.example", "owner@example.com"),
        owner_address="owner@example.com",
    )


def test_file_only_never_reaches_the_draft_path_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under file_only the mail question is never even ASKED.

    The proof is a booby-trapped ``should_draft``: if the posture gate let
    control through, the folder table would be consulted and this test would
    raise rather than fail an assertion. Asserting ``should_draft is False``
    alone would pass just as happily for a gate that ran the tables and then
    discarded the answer -- and that gate would still have inspected a thread
    on a deployment that had said not to.
    """

    def _boobytrap(folder: str, context: object) -> object:
        raise AssertionError(
            "the folder trigger table was consulted under file_only; "
            "the posture gate must refuse before any mail question"
        )

    monkeypatch.setattr(drafting, "should_draft", _boobytrap)

    # drafts_enabled is ON for both providers deliberately. With it off, this
    # test would pass for a gate that ignored posture entirely and read only
    # the flag -- the refusal has to be attributable to the POSTURE, so the
    # other half of the AND is set to the permissive value.
    config = ConnectorConfig(
        drafts_enabled={"google": True, "microsoft": True},
        posture=Posture.FILE_ONLY,
    )
    decision = drafting.may_draft(config, "google", "Business-Action", _drafting_context())

    assert decision.should_draft is False
    assert decision.reason == drafting.POSTURE_REFUSAL
    assert decision.deterministic is True


def test_file_read_draft_with_drafts_off_still_produces_no_draft() -> None:
    """The narrowing rule's load-bearing row. Posture asked; the flag refused.

    ``file_read_draft`` does not enable anything. ``drafts_enabled`` is what
    decides whether Gmail is asked for ``gmail.compose``, and a posture that
    could turn drafts on would be a second, weaker path to that decision.
    """
    config = ConnectorConfig(
        drafts_enabled={"google": False, "microsoft": False},
        posture=Posture.FILE_READ_DRAFT,
    )

    assert config.drafts_allowed("google") is False
    decision = may_draft(config, "google", "Business-Action", _drafting_context())
    assert decision.should_draft is False
    assert decision.reason == POSTURE_REFUSAL


def test_file_only_refuses_even_where_the_provider_flag_permits_drafts() -> None:
    """The other narrowing row: the client said no, the scope said yes.

    A positive control sits beside it, because an absence assertion with no
    paired presence assertion passes vacuously -- if ``may_draft`` refused
    unconditionally, the refusal above would prove nothing.
    """
    enabled = {"google": True, "microsoft": True}
    narrowed = ConnectorConfig(drafts_enabled=enabled, posture=Posture.FILE_ONLY)
    permitted = ConnectorConfig(drafts_enabled=enabled, posture=Posture.FILE_READ_DRAFT)
    context = _drafting_context()

    assert narrowed.drafts_allowed("google") is False
    assert may_draft(narrowed, "google", "Business-Action", context).should_draft is False

    # POSITIVE CONTROL. Same thread, same folder, same flags -- only the
    # posture differs, so a draft here proves the refusal above was the
    # posture and not a broken function.
    assert permitted.drafts_allowed("google") is True
    assert may_draft(permitted, "google", "Business-Action", context).should_draft is True
