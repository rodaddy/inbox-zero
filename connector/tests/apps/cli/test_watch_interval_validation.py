"""``watch --interval`` must be refused at the parser when it is not a real delay.

The defect this pins (#181): ``--interval`` was a bare ``type=float``, so ``0``,
``-5``, ``nan``, and ``inf`` all reached the polling loop. Zero and negatives
turn the watch into a tight loop that hammers the provider and the classifier
with no backoff; ``nan`` makes every sleep comparison false; ``inf`` is a watch
that polls once and never again while still reporting itself as running.

The floor belongs at the ARGUMENT boundary, not inside the loop: an operator
who typed the wrong number should get a usage error and a non-zero exit before
a single credential is read, not a daemon that appears to start.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.cli import main as cli


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf", "-inf", "0.5"])
def test_watch_refuses_an_interval_that_is_not_a_real_delay(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Argparse exits 2 for a usage error, and says so on stderr."""
    with pytest.raises(SystemExit) as caught:
        cli.main(["watch", "--interval", bad])
    assert caught.value.code == 2
    assert "--interval" in capsys.readouterr().err


@pytest.mark.parametrize(("good", "expected"), [("1", 1.0), ("5", 5.0), ("3600", 3600.0)])
def test_watch_accepts_an_interval_at_or_above_the_floor(good: str, expected: float) -> None:
    """The parser keeps every sane value and hands it through as a float."""
    args = cli.build_parser().parse_args(["watch", "--interval", good])
    assert args.interval == pytest.approx(expected)


def test_the_default_interval_survives_the_validator() -> None:
    """The default is a value, not a string, so it never passes through ``type``."""
    assert cli.build_parser().parse_args(["watch"]).interval == pytest.approx(60.0)
