"""Pin the loguru traceback spelling across the whole package.

loguru's ``logger.error`` does not understand stdlib's ``exc_info`` keyword: it
absorbs the kwarg into the record's ``extra`` dict and attaches no traceback, so
a failure line says something broke and never says what. The only spelling that
attaches the exception is ``logger.opt(exception=True).<level>(...)``, and this
test fails the build if the stdlib spelling reappears anywhere in the package.
"""

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "inbox_zero_connector"
FORBIDDEN = re.compile(r"exc_info\s*=")


def _scan() -> tuple[list[str], int]:
    """Return the offending ``path:line`` strings and the file count scanned."""
    offenders: list[str] = []
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{number}")
    return offenders, len(files)


def test_package_never_uses_the_stdlib_exc_info_spelling() -> None:
    offenders, _ = _scan()
    assert not offenders, (
        "loguru silently drops the stdlib exc_info keyword -- use "
        "logger.opt(exception=True).<level>(...) instead. Offenders: " + ", ".join(offenders)
    )


def test_scan_actually_examined_the_package() -> None:
    _, file_count = _scan()
    assert file_count > 50, (
        f"the scan only found {file_count} python files under {PACKAGE_ROOT}; "
        "an empty or mis-rooted walk would make the spelling test vacuous"
    )
