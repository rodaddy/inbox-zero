"""Microsoft Graph half of the mailbox-reset restore fixture.

DEV/TEST FIXTURE, NOT A SHIPPED FEATURE. Everything here exists only so a local
Graph watch experiment can be run twice from the same starting placement. None
of it is reachable from the installer, the kit docs, the ops manual, or the MCP
surface, and none of it may become a ``MailProvider`` method.

WHY THIS IS ITS OWN MODULE
    Split out of ``reset.py`` for the same reason ``gmail_history.py`` was split
    out of ``gmail.py``: ``reset.py`` measured 450 of the 500 code lines
    ``_DOCS/STANDARDS-python.md`` enforces before this work began, and the Graph
    restore mechanics do not fit in the remaining fifty. The alternative --
    compressing the docstrings that carry the fixture-only argument -- defeats
    the gate, because the ceiling counts CODE lines precisely so that the prose
    an auditor reads is never what gets deleted to make room.

    The seam is real rather than merely convenient. ``reset.py`` keeps the
    provider-neutral spine -- the snapshot document, the ledger window, the
    selection helpers, the mismatch computation, the report -- and dispatches
    into a provider half. Gmail's half is still inline; this is Graph's.

    The dependency runs ONE WAY at module scope: this module imports the shared
    vocabulary from ``reset``, and ``reset`` imports this one inside its single
    dispatch branch. Confining the deferred import to that one call site is what
    keeps the cycle from spreading a lazy import into every function here.

WHY PLACEMENT IS THE WHOLE UNDO
    The watcher's only write is ``file_message`` (``watcher.py:73``). On Graph
    that is a folder move and nothing else -- no category is set, no flag is
    changed -- so returning a message to its snapshot folder undoes the run
    completely. There is deliberately no category handling in this module; a
    restore that touched categories would be undoing a write the connector
    never made.

GRAPH'S /move IS COPY-THEN-DELETE, AND THE ID YOU HOLD DIES
    ``POST /me/messages/{id}/move`` does not relocate a message in place. Graph
    creates a copy in the destination folder and deletes the original, so the
    response carries a NEW id and the id used to issue the call is dead the
    moment it succeeds. That is why nothing here caches an id across the move
    loop and why the finish step re-enumerates the mailbox from scratch rather
    than trusting what the move responses reported. Re-reading is not defensive
    duplication -- it is the only way to learn where anything actually ended up.

    Graph answers ``404`` for an id that no longer resolves, which on this path
    means the message was deleted or moved by somebody else between enumeration
    and the write. That is a race in a fixture, not a failure of the fixture, so
    it is translated into the shared missing-message signal and the remaining
    messages still get their move.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from inbox_zero_connector.providers.limits import MAX_PAGE_SIZE
from inbox_zero_connector.providers.outlook.categories import GraphStatusError
from inbox_zero_connector.providers.outlook.provider import PROVIDER as GRAPH_PROVIDER_NAME
from inbox_zero_connector.providers.outlook.provider import OutlookProvider
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.reset.fixture import (
    CurrentMailbox,
    RestoreError,
    RestoreReport,
    SnapshotDocument,
    _container_index,
    _CurrentMessage,
    _details_by_message,
    _log_missing,
    _MessageMissingError,
    _placement_mismatches,
    graph_cursor,
)
from inbox_zero_connector.utils.logging_config import ProviderCall

GRAPH_RESTORE_FIELDS = "id,internetMessageId,parentFolderId"


def restore_graph(
    provider: OutlookProvider,
    document: SnapshotDocument,
    rows: tuple[ledger.LedgerEntry, ...],
) -> RestoreReport:
    """Undo Graph filings by moving each receipted message back to its snapshot folder."""
    current = current_mailbox(provider)
    missing = {identifier for identifier in document.messages if identifier not in current}
    _log_missing(missing)
    details = _details_by_message(rows, document.messages, current)
    modified = 0
    for internet_id in sorted(details):
        live = current.get(internet_id)
        if live is None:
            continue
        destination = document.messages[internet_id].containers[0]
        if live.containers == (destination,):
            continue
        try:
            # Graph's /move is copy-then-delete, so ``live.provider_id`` is dead
            # once this returns. Nothing reuses it: the finish step below
            # re-reads placement fresh rather than tracking ids across moves.
            restore_move(provider, internet_id, live.provider_id, destination)
        except _MessageMissingError:
            missing.add(internet_id)
            logger.warning(f"RESET: restore skipped missing internet_message_id={internet_id}")
            continue
        modified += 1
    return _finish(provider, document, rows, modified, missing)


def _finish(
    provider: OutlookProvider,
    document: SnapshotDocument,
    rows: tuple[ledger.LedgerEntry, ...],
    modified: int,
    missing: set[str],
) -> RestoreReport:
    """Re-read placement, include races, and build the content-free report."""
    after = current_mailbox(provider)
    newly_missing = {identifier for identifier in document.messages if identifier not in after}
    _log_missing(newly_missing - missing)
    missing.update(newly_missing)
    mismatches = _placement_mismatches(document.messages, after, missing)
    logger.info(
        f"RESET: restore complete applied_rows={len(rows)} modified={modified} "
        f"missing={len(missing)} mismatches={len(mismatches)}"
    )
    return RestoreReport(
        applied_rows=len(rows),
        modified=modified,
        missing=tuple(sorted(missing)),
        mismatches=mismatches,
    )


def current_mailbox(provider: OutlookProvider) -> CurrentMailbox:
    """Enumerate live Graph placement across every folder, keyed by stable id.

    The walk is the one ``reset._graph_folder_snapshot`` performs, with two
    differences the restore path needs and the snapshot path must not have. It
    selects ``id`` as well, because a move has to be addressed by the ephemeral
    provider id even though every decision is made on the stable one. And it
    covers EVERY folder rather than only in-scope ones, because a message the
    connector filed has by definition left the folder the snapshot recorded it
    in; restricting the re-read to the snapshot's scope would report a
    relocated message as vanished.
    """
    current: CurrentMailbox = {}
    for container in _container_index(provider).values():
        _folder_into(provider, container.id, current)
    return current


def _folder_into(provider: OutlookProvider, folder_id: str, current: CurrentMailbox) -> None:
    """Read every message page in one Graph folder into the shared current map."""
    params = _page_params()
    while True:
        page = provider._get(
            "me",
            "mailFolders",
            folder_id,
            "messages",
            params=params,
            operation="reset_restore_list",
        )
        for raw in page.get("value", []) or []:
            _record(current, raw)
        cursor = graph_cursor(page.get("@odata.nextLink"))
        if cursor is None:
            return
        params = {**_page_params(), **cursor}


def _record(current: CurrentMailbox, raw: dict[str, Any]) -> None:
    """Record one live Graph row by internet id and reject ambiguous duplicates."""
    internet_id = str(raw.get("internetMessageId", ""))
    if not internet_id:
        raise RestoreError(
            "A listed Graph message has no internetMessageId; no restore writes were attempted."
        )
    if internet_id in current:
        raise RestoreError(
            "Duplicate internetMessageId encountered; no further restore writes were attempted."
        )
    current[internet_id] = _CurrentMessage(
        provider_id=str(raw.get("id", "")),
        internet_message_id=internet_id,
        containers=(str(raw.get("parentFolderId", "")),),
    )


def _page_params() -> dict[str, Any]:
    """The fixed content-free Graph projection for every restore page."""
    return {"$top": MAX_PAGE_SIZE, "$select": GRAPH_RESTORE_FIELDS}


def restore_move(
    provider: OutlookProvider,
    internet_id: str,
    provider_id: str,
    destination_id: str,
) -> None:
    """TEST SCAFFOLDING ONLY: relocate a Graph message through its raw transport.

    This helper deliberately lives in the reset fixture and never becomes a
    ``MailProvider`` method. It is NOT ``provider.file_message``: that is the
    product's write, receipted into the ledger and covered by the connector's
    shipped contract, while this is fixture plumbing whose only job is to put a
    mailbox back the way a local experiment found it. Shipped connector
    surfaces must not gain a move-back.

    The call goes through ``provider._post`` so the same path validation and
    five-point observability that cover every other Graph write cover this one
    too, and so a fixture cannot reach httpx with a path it assembled itself.
    """
    with ProviderCall(GRAPH_PROVIDER_NAME, "reset_restore", message_id=internet_id):
        try:
            provider._post(
                "me",
                "messages",
                provider_id,
                "move",
                payload={"destinationId": destination_id},
                operation="reset_restore",
            )
        except GraphStatusError as exc:
            if exc.status == 404:
                raise _MessageMissingError(provider_id) from exc
            raise RestoreError(
                "Graph restore move failed; remaining placement mismatches were not hidden. "
                "ACTION REQUIRED: verify the credential and retry from the same snapshot."
            ) from exc
