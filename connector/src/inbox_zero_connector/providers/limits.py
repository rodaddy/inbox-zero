"""Page-size limits both adapters answer to.

Gmail's ``users.messages.list`` and Graph's ``$top`` both accept at most
a few hundred items per page, and both adapters clamped a caller's limit
with the same expression and the same constant. Held one per adapter the
two numbers could drift by an edit and nothing would notice; held here
they are one number, the way ``containers`` holds one vocabulary.
"""

from __future__ import annotations

from typing import Final

MAX_PAGE_SIZE: Final = 200
"""Upper bound on one page from either provider."""


def cap_page(limit: int) -> int:
    """Clamp a caller-supplied page size into ``[1, MAX_PAGE_SIZE]``."""
    return max(1, min(limit, MAX_PAGE_SIZE))
