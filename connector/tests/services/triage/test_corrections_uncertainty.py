"""A correction may not teach a rule that files into uncertainty (#168).

THE EXECUTED DEFECT: the refusal lived on ``LearnedRule`` as a literal
``folder == "Review"`` check. Since the taxonomy began declaring its own
uncertainty folder (#159) that check is wrong in both directions at once -- on a
mailbox whose interview renamed the folder to ``Triage-Me``, a rule targeting
the REAL uncertainty folder was accepted, and a rule targeting an ordinary
folder the mailbox happens to call ``Review`` was refused.

A pydantic validator is handed a row and never a taxonomy, so it cannot know
which name holds the role. The refusal moved to the seams that DO hold one.
This file proves the creation seam; ``test_classify_learned_pass.py`` proves the
application seam, and the pair is the whole enforcement.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.services.triage.corrections import apply_correction
from inbox_zero_connector.services.triage.taxonomy import Taxonomy

RENAMED = Taxonomy(
    folders=("Business-Action", "Review", "Triage-Me"),
    descriptions={},
    review_folder="Triage-Me",
)
"""Renamed uncertainty folder, AND an ordinary folder still spelled ``Review``."""


class ExplodingProvider:
    """Refusal happens before any mailbox call. Reaching one is the failure."""

    name = "test"

    def list_inbox(self, limit: int = 50) -> tuple[object, ...]:
        raise AssertionError("the provider was touched for a refused correction")

    def file_message(self, message_id: str, folder: str) -> str:
        raise AssertionError("a message was moved for a refused correction")


def test_a_correction_targeting_the_uncertainty_folder_is_refused() -> None:
    with pytest.raises(ValueError, match="learned to keep guessing"):
        apply_correction(
            ExplodingProvider(),  # type: ignore[arg-type]
            "m1",
            "Triage-Me",
            installed=RENAMED,
        )
