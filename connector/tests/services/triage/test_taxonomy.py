"""Taxonomy enforcement -- security invariant 2 (closed vocabulary)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inbox_zero_connector.services.triage.taxonomy import (
    MAX_FOLDERS,
    Taxonomy,
    TaxonomyError,
    UnknownFolderError,
    default_taxonomy,
    load_taxonomy,
    write_taxonomy,
)
from inbox_zero_connector.utils.paths import taxonomy_path


def test_validate_accepts_a_folder_in_the_vocabulary() -> None:
    taxonomy = default_taxonomy()
    assert taxonomy.validate_folder("Business-Action") == "Business-Action"


def test_validate_rejects_a_folder_outside_the_vocabulary() -> None:
    taxonomy = default_taxonomy()
    with pytest.raises(UnknownFolderError) as excinfo:
        taxonomy.validate_folder("Exfiltrate")
    assert excinfo.value.requested == "Exfiltrate"
    # The error names the legal set so the AI can retry legally rather than guess.
    assert "Business-Action" in str(excinfo.value)


def test_validate_is_case_sensitive() -> None:
    """'business-action' must not silently address the 'Business-Action' folder.

    Case-insensitive matching would mean one name maps to one folder on Gmail
    and two on Graph, so exactness keeps provider behaviour identical.
    """
    with pytest.raises(UnknownFolderError):
        default_taxonomy().validate_folder("business-action")


def test_validate_rejects_a_near_miss_with_whitespace() -> None:
    with pytest.raises(UnknownFolderError):
        default_taxonomy().validate_folder("Business-Action ")


def test_roundtrip_write_then_load(tmp_path: Path) -> None:
    original = Taxonomy(
        folders=("Alpha", "Beta"),
        descriptions={"Alpha": "a", "Beta": "b"},
        review_folder="Alpha",
    )
    write_taxonomy(original)
    loaded = load_taxonomy()
    assert loaded.folders == ("Alpha", "Beta")
    assert loaded.descriptions["Alpha"] == "a"


def test_load_raises_when_no_taxonomy_installed() -> None:
    """Absence must be loud, never a silent fallback to defaults.

    A silent default would file mail into folders the owner never agreed to.
    """
    with pytest.raises(TaxonomyError, match="No taxonomy installed"):
        load_taxonomy()


def test_load_rejects_invalid_json() -> None:
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(TaxonomyError, match="invalid JSON"):
        load_taxonomy()


def test_load_rejects_empty_folder_list() -> None:
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"folders": []}), encoding="utf-8")
    with pytest.raises(TaxonomyError, match="non-empty list"):
        load_taxonomy()


def test_load_rejects_duplicate_folder_names() -> None:
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"folders": ["Business-Action", "Business-Action"]}), encoding="utf-8"
    )
    with pytest.raises(TaxonomyError, match="duplicate"):
        load_taxonomy()


def test_load_rejects_an_oversized_taxonomy() -> None:
    """A corrupted config must not turn the closed vocabulary into an open one."""
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    folders = [f"F{i}" for i in range(MAX_FOLDERS + 1)]
    target.write_text(json.dumps({"folders": folders}), encoding="utf-8")
    with pytest.raises(TaxonomyError, match="limit is"):
        load_taxonomy()


def test_plain_string_folders_are_accepted() -> None:
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"folders": ["Business-Action", "Review"]}), encoding="utf-8")
    assert load_taxonomy().folders == ("Business-Action", "Review")


def test_taxonomy_module_exposes_no_runtime_widening_helper() -> None:
    """There must be no add/extend/append API the AI could reach.

    write_taxonomy exists for installation only and is not exposed by any MCP
    tool; anything named add_folder/extend would be a runtime widening path.
    """
    import inbox_zero_connector.services.triage.taxonomy as module

    for banned in ("add_folder", "extend", "append_folder", "create_folder"):
        assert not hasattr(module, banned)
        assert not hasattr(Taxonomy, banned)


EXAMPLE_TAXONOMY = (
    Path(__file__).resolve().parents[3]
    / "examples"
    / "taxonomy-business-personal.json"  # tests/services/triage -> connector/
)
"""The shipped business/personal taxonomy, installable as-is."""


def test_default_taxonomy_is_the_nine_folder_vocabulary() -> None:
    assert default_taxonomy().folders == (
        "Business-Action",
        "Business-Waiting",
        "Business-Reference",
        "Personal-Action",
        "Personal-Reference",
        "Receipts",
        "Newsletters",
        "Not-Interested",
        "Review",
    )


def test_every_default_folder_carries_a_real_description() -> None:
    """Descriptions are decision criteria the AI reads, not labels.

    A folder whose description merely restates its own name gives the
    classifier nothing to decide with, so assert real prose rather than mere
    presence.
    """
    taxonomy = default_taxonomy()
    for name in taxonomy.folders:
        description = taxonomy.descriptions[name]
        assert len(description) > 60, f"{name} description is too thin to classify against"
        assert description.strip() != name


def test_review_exists_so_the_classifier_can_decline_to_guess() -> None:
    """The uncertainty bucket is a safety property, not a convenience folder.

    Without somewhere to say "I do not know", an unsure classifier must force a
    confident answer, which either buries what mattered or floods Action with
    noise. Both teach the owner to stop trusting the system.
    """
    taxonomy = default_taxonomy()
    assert "Review" in taxonomy
    assert taxonomy.validate_folder("Review") == "Review"


def test_the_shipped_example_taxonomy_parses_through_the_real_load_path() -> None:
    """The example file must install as-is, via load_taxonomy, not a hand parse.

    A shipped taxonomy that the connector cannot actually load is a broken
    install nobody discovers until first run, so parse it the same way the
    running connector does.
    """
    assert EXAMPLE_TAXONOMY.exists(), f"missing shipped taxonomy at {EXAMPLE_TAXONOMY}"
    loaded = load_taxonomy(EXAMPLE_TAXONOMY)
    assert loaded.folders == default_taxonomy().folders
    assert loaded.descriptions == default_taxonomy().descriptions


def test_the_shipped_example_stays_inside_the_closed_vocabulary_limit() -> None:
    assert len(load_taxonomy(EXAMPLE_TAXONOMY).folders) <= MAX_FOLDERS


# ---------------------------------------------------------------------------
# The taxonomy declares its own uncertainty folder (issue #159)
# ---------------------------------------------------------------------------


def test_load_rejects_a_taxonomy_with_no_uncertainty_folder() -> None:
    """No place to say "I do not know" is a LOAD failure, not a filing failure.

    Before this, a taxonomy whose interview renamed Review parsed happily and
    the connector kept coercing into a folder the mailbox did not have -- so
    the defect surfaced at the provider, one layer and one day later, with
    nothing pointing back at the config that caused it.
    """
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"folders": ["Needs-Me", "Bulk"]}), encoding="utf-8")

    with pytest.raises(TaxonomyError) as excinfo:
        load_taxonomy()

    message = str(excinfo.value)
    # The operator is holding the file: the error must name it and the two fixes.
    assert str(target) in message
    assert "Review" in message
    assert "review_folder" in message


def test_an_explicit_marker_declares_a_renamed_uncertainty_folder() -> None:
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"folders": ["Needs-Me", "Triage-Me"], "review_folder": "Triage-Me"}),
        encoding="utf-8",
    )

    assert load_taxonomy().review_folder == "Triage-Me"


def test_a_marker_naming_an_absent_folder_is_refused() -> None:
    """A dangling marker is the exact defect the marker exists to prevent."""
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"folders": ["Needs-Me"], "review_folder": "Nowhere"}), encoding="utf-8"
    )

    with pytest.raises(TaxonomyError, match="Nowhere"):
        load_taxonomy()


def test_a_non_string_marker_is_refused() -> None:
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"folders": ["Review"], "review_folder": ["Review"]}), encoding="utf-8"
    )

    with pytest.raises(TaxonomyError, match="review_folder"):
        load_taxonomy()


def test_a_folder_named_review_declares_itself_without_a_marker() -> None:
    """Every taxonomy shipped so far stays valid without being rewritten."""
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"folders": ["Needs-Me", "Review"]}), encoding="utf-8")

    assert load_taxonomy().review_folder == "Review"


def test_the_marker_wins_over_a_folder_literally_named_review() -> None:
    target = taxonomy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"folders": ["Review", "Triage-Me"], "review_folder": "Triage-Me"}),
        encoding="utf-8",
    )

    assert load_taxonomy().review_folder == "Triage-Me"


def test_roundtrip_preserves_a_renamed_uncertainty_folder() -> None:
    """Write-then-load must return the taxonomy we had, not one re-derived by name."""
    original = Taxonomy(
        folders=("Alpha", "Beta"),
        descriptions={"Alpha": "a", "Beta": "b"},
        review_folder="Beta",
    )
    write_taxonomy(original)

    assert load_taxonomy().review_folder == "Beta"


def test_the_shipped_example_declares_review_as_its_uncertainty_folder() -> None:
    assert load_taxonomy(EXAMPLE_TAXONOMY).review_folder == "Review"
