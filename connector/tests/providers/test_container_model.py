"""The ``Container`` model, and the ``containers`` field it is addressed by.

``Container`` exists to make ONE structural difference explicit: a Graph message
sits in exactly one folder, a Gmail message routinely carries several labels.
These tests pin that both shapes construct, that the fields which are genuinely
absent on one provider accept None rather than demanding a placeholder, and that
adding ``containers`` to ``Message`` did not make placement mandatory.
"""

from __future__ import annotations

from inbox_zero_connector.providers.base import Container, Message


def _message(**overrides: object) -> Message:
    """A minimally valid inbox header, so each test states only what it tests."""
    fields: dict[str, object] = {
        "id": "m1",
        "thread_id": "t1",
        "sender": "someone@example.com",
        "subject": "Subject",
        "received": "2026-08-18T10:00:00Z",
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


def test_exclusive_container_constructs() -> None:
    """A Graph folder: exactly one such container holds any given message."""
    folder = Container(
        id="AAMkAGI2", name="Billing", well_known=None, parent_id="AAMkROOT", exclusive=True
    )

    assert folder.exclusive is True
    assert folder.parent_id == "AAMkROOT"


def test_non_exclusive_container_constructs() -> None:
    """A Gmail label: a message may hold many at once, so exclusive is False."""
    label = Container(
        id="Label_17", name="Clients/Acme", well_known=None, parent_id=None, exclusive=False
    )

    assert label.exclusive is False


def test_well_known_accepts_none_and_a_tag() -> None:
    """None is a user-created container; a tag is one the adapter resolved."""
    user_made = Container(
        id="Label_17", name="Acme", well_known=None, parent_id=None, exclusive=False
    )
    system = Container(id="SPAM", name="Spam", well_known="junk", parent_id=None, exclusive=False)

    assert user_made.well_known is None
    assert system.well_known == "junk"


def test_parent_id_accepts_none() -> None:
    """Gmail exposes no hierarchy, so parent_id must be optional, not a placeholder."""
    label = Container(
        id="Label_17", name="Clients/Acme", well_known=None, parent_id=None, exclusive=False
    )

    assert label.parent_id is None


def test_message_defaults_containers_to_empty() -> None:
    """Placement is optional: every existing caller constructs without it."""
    assert _message().containers == ()


def test_message_accepts_multiple_containers() -> None:
    """Gmail's answer is genuinely many, so the field must hold more than one."""
    message = _message(containers=("INBOX", "Label_17", "IMPORTANT"))

    assert message.containers == ("INBOX", "Label_17", "IMPORTANT")
