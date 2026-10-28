"""The one page bound both adapters clamp to, and the proof neither redefines it."""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType

import pytest

from inbox_zero_connector.providers import gmail, outlook
from inbox_zero_connector.providers.limits import MAX_PAGE_SIZE, cap_page


@pytest.mark.parametrize(
    ("asked", "expected"),
    [(0, 1), (1, 1), (200, 200), (201, 200), (-5, 1)],
)
def test_cap_page_clamps_into_range(asked: int, expected: int) -> None:
    """A caller's page size lands inside ``[1, MAX_PAGE_SIZE]``."""
    assert cap_page(asked) == expected


@pytest.mark.parametrize("package", [gmail, outlook])
def test_adapter_does_not_define_its_own_max_page_size(package: ModuleType) -> None:
    """Only ``providers.limits`` names the bound, so the two cannot drift."""
    source = (Path(str(package.__file__)).parent / "provider.py").read_text()
    assert re.search(r"^MAX_PAGE_SIZE\s*=", source, re.MULTILINE) is None
    assert MAX_PAGE_SIZE == 200
