"""``logging.console_level`` sets the stderr sink independently of the file sinks.

The operator case this exists for: a beta install wants a full DEBUG trace ON
DISK for support, while the person running the CLI sees a quiet terminal. Before
this key, one ``level`` drove all three sinks, so buying the disk trace meant
buying a wall of DEBUG on stderr.

The default is deliberately ``None`` rather than a literal level, so an existing
config that names only ``level`` keeps the exact behaviour it had: the console
sink falls back to ``level`` and nothing about that install changes.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from inbox_zero_connector.config import LoggingSettings
from inbox_zero_connector.utils import logging_config


def _levels(settings: LoggingSettings) -> list[int]:
    """Return the level number of every sink ``setup`` installed, in add order.

    Ordered, not classified by name. ``setup`` adds stderr first and the file
    sinks after, and loguru keeps handlers in insertion order, so index 0 is
    always the console. Matching on the sink's repr instead is what broke on
    Windows: the file sink's repr is not the plain ``str(tmp_path)`` the
    substring test looked for, so all three sinks were read as the console one.
    """
    logging_config.setup(settings, env="test")
    handlers = logger._core.handlers  # type: ignore[attr-defined]
    return [h.levelno for h in handlers.values()]


def test_console_level_defaults_to_level(tmp_path: Path) -> None:
    """Unset ``console_level`` leaves every sink on ``level`` -- no behaviour change."""
    settings = LoggingSettings(level="DEBUG", directory=tmp_path)
    assert settings.console_level is None
    assert set(_levels(settings)) == {logger.level("DEBUG").no}


def test_console_level_raises_only_the_stderr_sink(tmp_path: Path) -> None:
    """WARNING console + DEBUG file: the stderr sink is quiet, disk keeps the trace."""
    settings = LoggingSettings(level="DEBUG", console_level="WARNING", directory=tmp_path)
    console_level, *file_levels = _levels(settings)
    assert console_level == logger.level("WARNING").no
    assert file_levels, "expected file and json sinks"
    assert set(file_levels) == {logger.level("DEBUG").no}
