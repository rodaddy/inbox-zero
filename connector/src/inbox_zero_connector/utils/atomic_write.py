"""Atomic whole-file writes - tmp sibling, fsync, rename, or raise.

Three stores (watch state, voice profiles, learned rules) carried the same
fourteen-line body: mkdir the parent, write a temp file BESIDE the target,
flush and fsync, then ``replace``. The rename is only atomic within one
filesystem, so the temp file must share the directory; the fsync is what
stops a power loss from leaving a correctly named file with no contents.
Duplication across three packages is rung 3 of the promotion ladder, so the
body lives here once and each store keeps only its own serialisation and
its own log line.

Key Components:
    - write_text_atomic: the primitive. Returns the target path. On
      ``OSError`` it logs at ERROR with the exception attached and
      re-raises unchanged; the caller's log prefix names WHICH store.

Pattern/Convention:
    Callers serialise first and pass the finished string; this module
    never chooses a JSON layout, because the stores differ (the taxonomy
    file ends in a newline, the others do not) and on-disk bytes are a
    contract. Never write a credential through this function: tokens live
    in the OS keychain only.

Example:
    >>> from pathlib import Path
    >>> from inbox_zero_connector.utils import write_text_atomic
    >>> p = write_text_atomic(Path("/tmp/never-used-in-tests.json"), "{}")  # doctest: +SKIP

See Also:
    - inbox_zero_connector.services.watch.state: first adopter.
    - inbox_zero_connector.services.audit.ledger: append-only JSONL, which
      deliberately does NOT use this (it appends, never replaces).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path


def write_text_atomic(target: Path, text: str, *, what: str) -> Path:
    """Write ``text`` to ``target`` atomically; ``what`` names the store in the failure line."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)
    except OSError:
        logger.opt(exception=True).error(f"{what}: atomic write failed path={target}")
        raise
    return target
