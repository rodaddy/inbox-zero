"""Provider label / Outlook category name validation.

The constraint under test is mechanical, not stylistic: Outlook serialises the
categories on a message into ONE delimited string, so a name containing a comma
or a semicolon is split into two categories on write, with no error anywhere.
These tests exist so that fact stays enforced rather than remembered.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.services.triage.taxonomy import (
    FORBIDDEN_LABEL_CHARS,
    MAX_LABEL_CHARS,
    LabelNameError,
    validate_label_name,
)


def test_comma_is_rejected_because_outlook_would_split_the_name() -> None:
    with pytest.raises(LabelNameError) as excinfo:
        validate_label_name("rTech: Business, Action")
    assert "','" in str(excinfo.value)


def test_semicolon_is_rejected_because_outlook_would_split_the_name() -> None:
    with pytest.raises(LabelNameError) as excinfo:
        validate_label_name("rTech: Business; Action")
    assert "';'" in str(excinfo.value)


@pytest.mark.parametrize("variant", ["\u061b", "\ufe54", "\uff1b"])
def test_international_semicolon_variants_are_rejected(variant: str) -> None:
    with pytest.raises(LabelNameError):
        validate_label_name(f"rTech{variant} Business-Action")


def test_error_message_names_the_offending_character_and_its_consequence() -> None:
    """A bare 'invalid name' teaches the caller nothing about how to fix it."""
    with pytest.raises(LabelNameError) as excinfo:
        validate_label_name("Bills, Invoices")
    message = str(excinfo.value)
    assert "','" in message
    assert "U+002C" in message
    assert "split" in message.lower()


def test_colon_is_accepted() -> None:
    assert validate_label_name("rTech: Business-Action") == "rTech: Business-Action"


def test_hyphen_is_accepted() -> None:
    assert validate_label_name("Business-Action") == "Business-Action"


def test_empty_name_is_rejected() -> None:
    with pytest.raises(LabelNameError):
        validate_label_name("")


@pytest.mark.parametrize("blank", [" ", "\t", "   \n  "])
def test_whitespace_only_name_is_rejected(blank: str) -> None:
    with pytest.raises(LabelNameError):
        validate_label_name(blank)


def test_surrounding_whitespace_is_stripped() -> None:
    """A trailing space is invisible in a config file and yields a lookalike name."""
    assert validate_label_name("  rTech: Business-Action  ") == "rTech: Business-Action"


def test_over_length_name_is_rejected() -> None:
    with pytest.raises(LabelNameError) as excinfo:
        validate_label_name("A" * (MAX_LABEL_CHARS + 1))
    assert str(MAX_LABEL_CHARS) in str(excinfo.value)


def test_a_name_at_the_ceiling_is_accepted() -> None:
    """The boundary is inclusive; without this the over-length test also passes off-by-one."""
    name = "A" * MAX_LABEL_CHARS
    assert validate_label_name(name) == name


def test_length_is_measured_after_stripping() -> None:
    """Otherwise padding a legal name with spaces would fail the ceiling."""
    padded = "  " + "A" * MAX_LABEL_CHARS + "  "
    assert validate_label_name(padded) == "A" * MAX_LABEL_CHARS


def test_forbidden_set_contains_both_outlook_delimiters() -> None:
    assert {",", ";"} <= FORBIDDEN_LABEL_CHARS
