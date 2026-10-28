"""``inbox-zero-connector paths`` states the namespace before the paths.

An operator supporting two installs on one machine reaches for this command
first, and the question it has to answer is "which install am I looking at".
Reading that off the directory strings means guessing which trailing segment
is the namespace and which is part of the OS-standard layout -- so the id is
printed as its own line, first, including when it is empty. A blank value is
a stated fact; an omitted line is something somebody has to interpret.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.cli.main import main
from inbox_zero_connector.config_paths import INSTALL_ID_ENV, reset_install_id_cache


@pytest.fixture(autouse=True)
def _fresh_install_id_cache() -> None:
    """The layer-1 read is process-cached; each test starts cold."""
    reset_install_id_cache()


def test_paths_prints_a_blank_install_id_line_first(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no id the line is still there, and it is still first."""
    monkeypatch.delenv(INSTALL_ID_ENV, raising=False)

    assert main(["paths"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "install id: "
    assert lines[1].startswith("config dir: ")


def test_paths_prints_the_install_id(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an id set, ``paths`` names it."""
    monkeypatch.setenv(INSTALL_ID_ENV, "sample-install")

    assert main(["paths"]) == 0

    assert capsys.readouterr().out.splitlines()[0] == "install id: sample-install"
