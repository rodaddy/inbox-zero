"""The MCP ``create_draft`` path is gated on POSTURE, not on the scope flag.

Its own module rather than a case appended to ``test_server_tools.py`` for the
reason the size ceiling exists: that file is at its limit, and the property
under test here is one claim about one seam, which is exactly the unit a module
is for.

The distinction this file defends: ``ConnectorConfig.drafts_on`` answers the
SCOPE question -- did this deployment ask Google for ``gmail.compose`` -- and
``drafts_allowed`` answers the POSTURE question -- may a draft actually be
composed. ``services/voice/drafting.may_draft`` reads the second one. The MCP
tool read the FIRST one, so a ``file_only`` client whose install had once set
the scope flag could draft through MCP while the watcher refused the same
thread. The two accessors stay separate; only this call site moved.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.server import main as server
from inbox_zero_connector.config import ConfigError, ConnectorConfig, Posture, VoiceRouting


class _RefusingProvider:
    """Any call is a failure. The gate must refuse before the provider is touched.

    A stub rather than the ``SpyProvider`` from ``test_server_tools`` because
    ``tests/apps/server`` is not a package, and importing across test modules
    would make this file depend on another module's fixture wiring to assert a
    property that needs no provider behaviour at all.
    """

    name = "google"

    def __getattr__(self, attr: str) -> object:
        raise AssertionError(f"a refused draft must not reach the provider ({attr})")


def test_create_draft_is_refused_when_posture_forbids_drafting(
    installed_taxonomy: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope flag set, posture ``file_only``: the pair where the two disagree."""
    monkeypatch.setattr(server, "_provider", _RefusingProvider())
    monkeypatch.setattr(server, "_owner", "owner@example.com")
    monkeypatch.setattr(
        server,
        "_config",
        ConnectorConfig(
            posture=Posture.FILE_ONLY,
            drafts_enabled={"google": True, "microsoft": True},
            routing=VoiceRouting(owner_domain="example.com", sales_labels=("Prospects",)),
        ),
    )
    with pytest.raises(ConfigError, match="disabled"):
        server.create_draft("t1", "hi")
