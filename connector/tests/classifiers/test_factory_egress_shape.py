"""The classifier's egress is the documented four, and nothing wired-but-dead (#166).

``owner_domain`` was plumbed through the constructor and into ``build_prompt``
and NEVER SUPPLIED: ``factory.from_settings`` is the only construction site in
the package and passes four arguments, so ``self.owner_domain`` was ``""`` in
every deployment and the clause never rendered. Configuration with no consumer
is the exact defect ``factory`` itself was created to end (#115) -- it reads as
a working feature and produces no error.

These tests pin the shape that is actually egressed: sender, subject, snippet,
and the installed taxonomy vocabulary. Wiring the owner's domain deliberately
is a decision recorded on #166, and when it is taken it arrives through
``from_settings`` with a test that watches the wire, not as an unset default.
"""

from __future__ import annotations

import inspect

from inbox_zero_connector.classifiers import factory
from inbox_zero_connector.classifiers.openai_compatible import (
    OpenAICompatibleClassifier,
    build_prompt,
)


def test_the_constructor_carries_no_unsupplied_owner_domain() -> None:
    assert "owner_domain" not in inspect.signature(OpenAICompatibleClassifier.__init__).parameters


def test_build_prompt_carries_no_unsupplied_owner_domain() -> None:
    assert "owner_domain" not in inspect.signature(build_prompt).parameters


def test_the_factory_source_names_no_owner_domain() -> None:
    """The dead plumbing is gone from the seam, and #166 records the decision."""
    source = inspect.getsource(factory)
    assert "owner_domain" not in source.replace("#166", "")
    assert "#166" in source
