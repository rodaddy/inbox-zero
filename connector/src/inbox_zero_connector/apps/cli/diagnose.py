"""The ``diagnose`` subcommand: the content-free diagnostics bundle.

A beta tester who says "it did not file that message" is describing something
the logs alone cannot settle. This verb packages everything that CAN settle it
into one zip they mail back: the per-message decision trace, the audit ledger,
the watch state, the installed config and taxonomy, the LIVE scope manifest,
the last seven days of digests, the version triple, and the tail of each log
file. One attachment, no instructions, no screenshots of a terminal. Install
transcripts are the exception to the tail: they ship whole, because a
transcript is the install record and the failure is usually at its start.

WHAT IT NEVER CONTAINS, BY CONSTRUCTION:

* ``secrets.json``. It is not in the member list, so there is no code path
  that adds it -- an exclusion filter would be a rule somebody could get wrong.
* Any token. Tokens live only in the OS keychain and are never written to disk
  by this connector, so no file this module can reach holds one.
* Any mail content. The trace and the ledger are content-free by design (see
  ``services.audit.trace``): a sender is already reduced to a DOMAIN before it
  is written, and a model-produced reason is truncated at the boundary.

Belt and braces on top of that: every credential VALUE this install can see is
scrubbed out of every text member before it is written. The vocabulary is drawn
from ``secrets.json`` AND from the credential fields of
``connector-config.json``, and the second source is the one that matters most --
``secrets.json`` is structurally never a member, while ``connector-config.json``
IS one, so a secret a human typed into the wrong file was the only value that
could actually ship. A log line that quoted an API key -- which nothing here
does deliberately, and which is exactly the sort of thing a diagnostics bundle
would otherwise carry off the machine -- can therefore not ship either.

Stdout is this command's product, the same T20 exemption ``show_scopes.py``
carries and for the same reason: a CLI cannot deliver a file path through the
logger, which writes to sinks. It is CLI-only and never runs in the server
process, so nothing it prints can reach the MCP protocol channel.

Lives outside ``main.py`` for the code-line ceiling, like ``desktop.py``,
``setup.py``, and ``show_scopes.py`` before it.
"""

from __future__ import annotations

import contextlib
import io
import json
import platform
import zipfile
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Final

from loguru import logger

from inbox_zero_connector import __version__
from inbox_zero_connector.apps.cli import show_scopes
from inbox_zero_connector.config import (
    PROJECT_ROOT,
    config_path,
    data_dir,
    ledger_path,
    secrets_path,
    settings,
    taxonomy_path,
)
from inbox_zero_connector.services.audit import ledger, trace

LOG_TAIL_LINES: Final = 500
"""Lines kept from each log file. A bundle is a tail, not a log archive."""

INSTALL_LOG_GLOB: Final = "install-*.log"
"""Install transcripts, written by ``install.sh``/``install.ps1`` under the root."""

DIGEST_DAYS: Final = 7
"""Days of digest rendered. One week covers a beta tester's "last week it..."."""

REDACTED: Final = "[REDACTED]"

LOG_GLOBS: Final[tuple[str, ...]] = ("*.log", "*.jsonl")
"""Both halves of the log directory. ``logging_config`` writes a human-readable
``.log`` and a structured ``.jsonl`` side by side, and the ``.jsonl`` is the one
support actually parses -- a ``*.log``-only glob shipped the weaker copy."""

NEVER_INCLUDED: Final[tuple[str, ...]] = (
    "secrets.json",
    "tokens (OS keychain only)",
    "mail content (subject/body/sender address)",
    "any credential value known to this install (scrubbed from every member)",
)
"""Stated IN the bundle, so the recipient reads the promise beside the evidence."""

CONFIG_CREDENTIAL_KEY: Final = "credentials"
"""The one ``connector-config.json`` group whose values are scrub vocabulary.

Scoped to the group rather than the whole file on purpose: the rest of that file
is posture, routing, and folder names, and feeding those to ``str.replace``
would redact ordinary words out of every log line in the bundle."""


def _secret_values() -> tuple[str, ...]:
    """Every credential VALUE this install can see, for scrubbing. Never logged.

    Two sources, and the second is the important one. ``secrets.json`` is where
    a value is SUPPOSED to live and is structurally never a member. The
    ``credentials`` group of ``connector-config.json`` is where a value should
    never live -- but that file IS a member, so a secret typed into the wrong
    place was the only one that could actually leave the machine.

    Returns an empty tuple's worth of contribution from either source on ANY
    failure -- absent file, bad JSON, unreadable permissions. A diagnostics
    bundle whose redaction pass raised would be a bundle that never got written,
    and a bundle from a broken install is exactly when one is needed.
    """
    found: list[str] = []
    _collect_strings(_credential_source(secrets_path(), whole=True), found)
    _collect_strings(_credential_source(config_path(), whole=False), found)
    return tuple(sorted(set(found), key=len, reverse=True))


def _credential_source(target: Path, *, whole: bool) -> object:
    """Parse one scrub source, or ``None`` when it cannot be read.

    Args:
        target: The file to read.
        whole: Take the entire document (``secrets.json``, which holds nothing
            else) rather than only its ``credentials`` group.

    Returns:
        The parsed object to harvest strings from, or ``None``.
    """
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError, ValueError:
        # DOCUMENTED FAIL-OPEN: this redaction source is unreadable, so it
        # contributes nothing. Logged with the PATH and the exception TYPE only;
        # the file's contents are the secrets.
        logger.opt(exception=True).warning(
            f"DIAGNOSE: scrub source unreadable path={target}; no values from it"
        )
        return None
    if whole:
        return raw
    return raw.get(CONFIG_CREDENTIAL_KEY) if isinstance(raw, dict) else None


def _collect_strings(node: object, found: list[str]) -> None:
    """Walk dicts and lists, gathering every non-empty string leaf."""
    if isinstance(node, str):
        if node:
            found.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_strings(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_strings(value, found)


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every known secret value with ``[REDACTED]``."""
    for value in secrets:
        text = text.replace(value, REDACTED)
    return text


def _add(archive: zipfile.ZipFile, name: str, text: str, secrets: tuple[str, ...]) -> None:
    """Write one text member, scrubbed. The ONLY way a member is added."""
    archive.writestr(name, _scrub(text, secrets))


def _read(path: Path) -> str | None:
    """Read a file, or ``None`` when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # DOCUMENTED FAIL-OPEN: an unreadable installed file drops that one
        # member and the bundle still ships; the manifest shows the gap.
        logger.opt(exception=True).warning(f"DIAGNOSE: member unreadable, skipped path={path}")
        return None


def _show_scopes_text() -> str:
    """Capture ``show-scopes`` stdout, or a TYPE NAME when it cannot run.

    The exception message is deliberately dropped: it can quote a config value.
    The catch is broad because a bundle from a BROKEN install is exactly when
    one is most needed -- a missing or invalid config must degrade this member,
    never the zip.
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            show_scopes.cmd_show_scopes()
    except Exception as exc:  # ruff: ignore[blind-except]
        # DOCUMENTED FAIL-OPEN: the member degrades to a type name (see the
        # docstring); the zip still ships. Type only in the log as well.
        logger.warning(f"DIAGNOSE: show-scopes unavailable error_type={type(exc).__name__}")
        return f"show-scopes unavailable: {type(exc).__name__}"
    return buffer.getvalue()


def _digest_text() -> str:
    """The last seven days of digests, oldest first, each under its own header."""
    today = datetime.now(UTC).date()
    parts: list[str] = []
    for offset in range(DIGEST_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        parts.append(f"## {day.isoformat()}\n{ledger.daily_digest(day)}\n")
    return "".join(parts)


def _versions_text() -> str:
    """What was running and on what. The first question support asks."""
    return "\n".join((
        f"connector {__version__}",
        f"python {platform.python_version()}",
        f"os {platform.platform()}",
        f"machine {platform.machine()}",
    ))


def _log_directory() -> Path | None:
    """The configured log directory, or ``None`` when settings cannot load."""
    try:
        return Path(settings().logging.directory)
    except Exception as exc:  # ruff: ignore[blind-except]
        # DOCUMENTED FAIL-OPEN: no settings means no log directory to tail;
        # the rest of the bundle still ships.
        logger.warning(f"DIAGNOSE: log directory unavailable error_type={type(exc).__name__}")
        return None


def _add_installed_files(archive: zipfile.ZipFile, secrets: tuple[str, ...]) -> None:
    """Add the on-disk artifacts that exist. Absence is silent and normal."""
    candidates: list[tuple[str, Path]] = [
        (trace.TRACE_FILENAME, trace.trace_path()),
        ("ledger.jsonl", ledger_path()),
    ]
    candidates.extend((state.name, state) for state in sorted(data_dir().glob("watchstate-*.json")))
    candidates.append(("connector-config.json", config_path()))
    candidates.append(("taxonomy.json", taxonomy_path()))
    for name, path in candidates:
        text = _read(path) if path.exists() else None
        if text is not None:
            _add(archive, name, text, secrets)


def _add_install_logs(archive: zipfile.ZipFile, secrets: tuple[str, ...]) -> None:
    """Add every install transcript WHOLE. Absence is silent and normal.

    Unlike a running log, an install transcript is a bounded, one-shot record
    of the event support is being asked about, so tailing it would cut off the
    beginning -- which is where an install goes wrong.
    """
    for path in sorted((PROJECT_ROOT / "logs").glob(INSTALL_LOG_GLOB)):
        text = _read(path)
        if text is not None:
            _add(archive, f"logs/{path.name}", text, secrets)


def _add_logs(archive: zipfile.ZipFile, secrets: tuple[str, ...]) -> None:
    """Add the last ``LOG_TAIL_LINES`` of each log file, scrubbed.

    Both sinks in ``LOG_GLOBS``, because the structured ``.jsonl`` is the copy a
    support engineer parses and a ``*.log``-only glob left it behind.

    Install transcripts are skipped here: ``_add_install_logs`` has already
    added them whole, and a log directory that coincides with the project root's
    would otherwise write the same member name twice.
    """
    directory = _log_directory()
    if directory is None or not directory.is_dir():
        return
    paths = sorted({path for glob in LOG_GLOBS for path in directory.glob(glob)})
    for path in paths:
        if fnmatch(path.name, INSTALL_LOG_GLOB):
            continue
        text = _read(path)
        if text is None:
            continue
        tail = text.splitlines()[-LOG_TAIL_LINES:]
        _add(archive, f"logs/{path.name}", "\n".join(tail) + "\n" if tail else "", secrets)


def _manifest_text(archive: zipfile.ZipFile, stamp: str) -> str:
    """Describe every member already written, so none can go unlisted."""
    members = [{"name": info.filename, "bytes": info.file_size} for info in archive.infolist()]
    return json.dumps(
        {
            "generated_at": stamp,
            "connector_version": __version__,
            "members": members,
            "never_included": list(NEVER_INCLUDED),
        },
        indent=2,
        sort_keys=True,
    )


def cmd_diagnose(out: Path | None) -> int:
    """Write the diagnostics zip and print where it landed.

    Args:
        out: Directory for the zip. ``None`` means the current directory,
            which is where a tester running one pasted command is standing.

    Returns:
        ``0``. A bundle from a broken install is still a bundle.
    """
    out_dir = out if out is not None else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"inbox-zero-diagnostics-{stamp}.zip"
    secrets = _secret_values()

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        _add_installed_files(archive, secrets)
        _add(archive, "show-scopes.txt", _show_scopes_text(), secrets)
        _add(archive, "digest-7d.txt", _digest_text(), secrets)
        _add(archive, "versions.txt", _versions_text(), secrets)
        _add_install_logs(archive, secrets)
        _add_logs(archive, secrets)
        count = len(archive.infolist())
        archive.writestr("manifest.json", _manifest_text(archive, stamp))

    logger.info(f"DIAGNOSE: bundle written path={target} members={count + 1}")
    print(target)
    print(f"{count + 1} members; manifest.json lists them.")
    print("Contains no mail content, no tokens, and no secrets.json.")
    print(
        "It DOES contain connector-config.json; every credential value this install "
        "can see is scrubbed from every member. Send this file to rTech."
    )
    return 0
