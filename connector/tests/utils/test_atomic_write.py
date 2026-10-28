"""The atomic writer's contract: whole-file replace, no temp left behind, failure logged and re-raised."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from inbox_zero_connector.utils import write_text_atomic


def test_writes_content_and_leaves_no_temp_file(tmp_path: Path) -> None:
    """The target holds exactly what was passed, the sibling temp file is gone, and the target is returned."""
    target = tmp_path / "f.json"

    returned = write_text_atomic(target, "abc", what="TESTSTORE")

    assert target.read_text(encoding="utf-8") == "abc"
    assert not (tmp_path / "f.json.tmp").exists()
    assert returned == target


def test_creates_missing_parent_directories(tmp_path: Path) -> None:
    """A target two levels below an existing directory gets its parents made for it."""
    target = tmp_path / "one" / "two" / "f.json"

    write_text_atomic(target, "{}", what="TESTSTORE")

    assert target.read_text(encoding="utf-8") == "{}"


def test_replaces_existing_content_whole(tmp_path: Path) -> None:
    """A second write replaces the file entirely rather than appending to it."""
    target = tmp_path / "f.json"

    write_text_atomic(target, "old", what="TESTSTORE")
    write_text_atomic(target, "new", what="TESTSTORE")

    assert target.read_text(encoding="utf-8") == "new"


def test_failure_logs_with_exception_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError mid-write is logged at ERROR with the traceback bound, re-raised, and leaves no target."""
    target = tmp_path / "f.json"

    def boom(_fd: int) -> None:
        raise OSError("disk")

    monkeypatch.setattr("inbox_zero_connector.utils.atomic_write.os.fsync", boom)

    records: list[Any] = []
    handle = logger.add(records.append, level="ERROR", format="{message}")
    try:
        with pytest.raises(OSError):
            write_text_atomic(target, "abc", what="TESTSTORE")
    finally:
        logger.remove(handle)

    assert len(records) == 1
    assert "TESTSTORE: atomic write failed" in records[0].record["message"]
    assert records[0].record["exception"] is not None
    assert not target.exists()
