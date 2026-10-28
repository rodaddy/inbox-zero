"""The four names both halves of the keystone need, and nothing else.

WHY THIS FILE EXISTS AT ALL
    ``config_paths`` resolves directories and ``config`` builds ``Settings``,
    and both need the app identity and the checkout root. ``config`` already
    imports ``config_paths``, so ``config_paths`` cannot import back without a
    cycle. Putting the shared constants in a leaf module both can import is the
    smallest fix that keeps ONE definition of each -- duplicating ``APP_NAME``
    or ``PROJECT_ROOT`` would let the two halves disagree about which app's
    directory or which checkout they are talking about, and a drifted
    ``PROJECT_ROOT`` in particular would resolve config layers from one tree
    while resolving install identity from another.

    Constants only, by design: no environment reads, no file reads, no logic.
    The keystone LAW is about who READS configuration, and this module reads
    nothing -- it names things. ``config`` re-exports these, so no caller
    outside the keystone imports this module directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

APP_NAME: Final = "inbox-zero-connector"
APP_AUTHOR: Final = "rTech"

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
"""The connector checkout root, so a fresh clone runs from its committed ``config/``."""

ENV_PREFIX: Final = "INBOX_ZERO_"
