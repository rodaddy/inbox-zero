"""Developer and test mailbox-reset fixture.

DEV/TEST FIXTURE, NOT A SHIPPED FEATURE. This module exists only to make local
watch experiments repeatable. It is not for client mailboxes and must not be
exposed through the connector's shipped surfaces.

Cursor reset delegates to ``WatchState.resync_required``. Snapshot enumeration
uses each provider's native listing surface because ``list_inbox`` is both
inbox-only and one-page. The JSON value model has exactly two fields, so message
content cannot enter a snapshot by an accidental model dump.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from googleapiclient.errors import HttpError
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from inbox_zero_connector.models.mail import Container
from inbox_zero_connector.providers.base import MailProvider
from inbox_zero_connector.providers.gmail.provider import INBOX_LABEL, GmailProvider
from inbox_zero_connector.providers.gmail.provider import PROVIDER as GMAIL_PROVIDER_NAME
from inbox_zero_connector.providers.limits import MAX_PAGE_SIZE
from inbox_zero_connector.providers.outlook.provider import OutlookProvider
from inbox_zero_connector.providers.retry import with_retry
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState
from inbox_zero_connector.utils.logging_config import ProviderCall

DEFAULT_SNAPSHOT_LIMIT = 500
GRAPH_SNAPSHOT_FIELDS = "internetMessageId,parentFolderId"
GMAIL_SNAPSHOT_HEADERS = ("Message-Id",)
GRAPH_CURSOR_KEYS = ("$skiptoken", "$skip")


class SnapshotError(RuntimeError):
    """A snapshot cannot be proven complete and safe to write."""


class RestoreError(RuntimeError):
    """A restore cannot safely identify or undo its run window."""


class SnapshotMessage(BaseModel):
    """The complete on-disk value for one message, with content fields impossible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    internet_message_id: str = Field(min_length=1)
    containers: tuple[str, ...] = Field(min_length=1)


Snapshot = dict[str, SnapshotMessage]
SnapshotEnumerator = Callable[[MailProvider, MailboxScope, int], Snapshot]


class SnapshotDocument(BaseModel):
    """A placement snapshot plus the append-only ledger boundary for its run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    ledger_entries: int = Field(ge=0)
    messages: Snapshot

    @model_validator(mode="after")
    def _stable_keys_match(self) -> SnapshotDocument:
        """Reject a document whose lookup key disagrees with its stable id."""
        if any(key != message.internet_message_id for key, message in self.messages.items()):
            raise ValueError("snapshot message keys must equal internet_message_id")
        return self


class RestoreReport(BaseModel):
    """Content-free outcome of one restore fixture run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied_rows: int
    modified: int
    missing: tuple[str, ...] = ()
    mismatches: tuple[str, ...] = ()


class _CurrentMessage(BaseModel):
    """One live Gmail placement, including its ephemeral transport id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    internet_message_id: str
    containers: tuple[str, ...]


class _MessageMissingError(RuntimeError):
    """A Gmail message disappeared between enumeration and fixture mutation."""


CurrentMailbox = dict[str, _CurrentMessage]


def reset_container_cursor(state: WatchState, container_id: str) -> int:
    """Reset one container cursor and return its new generation."""
    return state.resync_required(container_id)


def reset_all_cursors(state: WatchState) -> dict[str | None, int]:
    """Reset every stored cursor and return each stream's new generation."""
    container_ids = tuple(cursor.container_id for cursor in state.cursors.values())
    return {container_id: state.resync_required(container_id) for container_id in container_ids}


def write_snapshot(
    provider: MailProvider,
    scope: MailboxScope,
    output: Path,
    limit: int = DEFAULT_SNAPSHOT_LIMIT,
) -> int:
    """Write a complete content-free placement snapshot and return its message count.

    Raises:
        SnapshotError: The limit is invalid, enumeration cannot be completed
            safely, or the destination cannot be written.
    """
    if limit < 1:
        raise SnapshotError("Snapshot limit must be at least 1; no file was written.")
    ledger_entries = len(ledger.read_all())
    messages = _enumerate(provider, scope, limit)
    document = SnapshotDocument(
        provider=provider.name,
        ledger_entries=ledger_entries,
        messages=messages,
    )
    payload = document.model_dump(mode="json")
    try:
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(
            f"Could not write snapshot to {output}. ACTION REQUIRED: choose a writable path."
        ) from exc
    return len(messages)


def restore_snapshot(provider: MailProvider, source: Path) -> RestoreReport:
    """Undo APPLIED ledger writes after ``source`` and verify snapshot placement.

    The ledger selects the writes. The snapshot only bounds the run, prevents a
    pre-existing label from being removed, and supplies the placement used for
    the post-undo verification.

    Raises:
        RestoreError: The snapshot is unreadable, belongs to another provider,
            its ledger boundary is no longer valid, or the provider is unsupported.
        SnapshotError: Placement enumeration failed.
    """
    document = _read_snapshot(source)
    if document.provider != provider.name:
        raise RestoreError(
            f"Snapshot provider {document.provider!r} does not match active provider "
            f"{provider.name!r}; no mailbox changes were made."
        )
    rows = ledger.read_all()
    if len(rows) < document.ledger_entries:
        raise RestoreError(
            "The ledger has fewer readable rows than the snapshot boundary; no mailbox "
            "changes were made. ACTION REQUIRED: take a new snapshot."
        )
    applied = _applied_rows(rows[document.ledger_entries :], document)
    if isinstance(provider, GmailProvider):
        return _restore_gmail(provider, document, applied)
    if isinstance(provider, OutlookProvider):
        # Imported HERE, not at module scope: ``reset_graph`` imports this
        # module's shared vocabulary at ITS module scope, so a top-level import
        # in this direction would be a cycle. Confining the deferred import to
        # this one branch keeps every other import in both files ordinary.
        from inbox_zero_connector.services.reset import graph as reset_graph

        return reset_graph.restore_graph(provider, document, applied)
    raise RestoreError(
        f"Restore does not support provider type {type(provider).__name__}; "
        "no mailbox changes were made."
    )


def _read_snapshot(source: Path) -> SnapshotDocument:
    """Load and validate one restore document without accepting legacy ambiguity."""
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        return SnapshotDocument.model_validate(raw)
    except OSError as exc:
        raise RestoreError(
            f"Could not read snapshot {source}. ACTION REQUIRED: choose a readable file."
        ) from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RestoreError(
            "Snapshot is not a valid reset document; no mailbox changes were made. "
            "ACTION REQUIRED: take a new snapshot with this connector version."
        ) from exc


def _applied_rows(
    rows: list[ledger.LedgerEntry], document: SnapshotDocument
) -> tuple[ledger.LedgerEntry, ...]:
    """Select every successful connector filing from this snapshot's run window."""
    return tuple(
        row
        for row in rows
        if row.provider == document.provider
        and row.action == ledger.ACTION_FILE
        and row.status is ledger.Status.APPLIED
    )


def _restore_gmail(
    provider: GmailProvider,
    document: SnapshotDocument,
    rows: tuple[ledger.LedgerEntry, ...],
) -> RestoreReport:
    """Undo Gmail labels and de-inboxing through fixture-local raw API calls."""
    current = _gmail_current_mailbox(provider)
    missing = {identifier for identifier in document.messages if identifier not in current}
    _log_missing(missing)
    labels = _container_index(provider)
    details = _details_by_message(rows, document.messages, current)
    modified = 0
    for internet_id, connector_labels in sorted(details.items()):
        live = current.get(internet_id)
        if live is None:
            continue
        body = _gmail_undo_body(
            provider, live, document.messages[internet_id], connector_labels, labels
        )
        if not body:
            continue
        try:
            _gmail_restore_modify(provider, internet_id, live.provider_id, body)
        except _MessageMissingError:
            missing.add(internet_id)
            logger.warning(f"RESET: restore skipped missing internet_message_id={internet_id}")
            continue
        modified += 1
    return _finish_gmail_restore(provider, document, rows, modified, missing)


def _finish_gmail_restore(
    provider: GmailProvider,
    document: SnapshotDocument,
    rows: tuple[ledger.LedgerEntry, ...],
    modified: int,
    missing: set[str],
) -> RestoreReport:
    """Re-read placement, include races, and build the content-free report."""
    after = _gmail_current_mailbox(provider)
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


def _details_by_message(
    rows: tuple[ledger.LedgerEntry, ...],
    snapshot: Snapshot,
    current: CurrentMailbox,
) -> dict[str, frozenset[str]]:
    """Normalize receipts to internet ids, then collapse duplicate writes.

    Current ledger writers still receipt Gmail's transport id. That id is used
    only to locate the live raw row which supplies ``internet_message_id``;
    snapshot membership and every restore comparison use the stable id.
    """
    provider_ids = {message.provider_id: internet_id for internet_id, message in current.items()}
    details: dict[str, set[str]] = {}
    for row in rows:
        internet_id = (
            row.message_id if row.message_id in snapshot else provider_ids.get(row.message_id)
        )
        if internet_id not in snapshot:
            continue
        details.setdefault(internet_id, set()).add(row.detail)
    return {message_id: frozenset(names) for message_id, names in details.items()}


def _gmail_undo_body(
    provider: GmailProvider,
    live: _CurrentMessage,
    snapshot: SnapshotMessage,
    connector_labels: frozenset[str],
    labels: dict[str, Container],
) -> dict[str, list[str]]:
    """Build one additive Gmail modify body from ledger writes and snapshot guards."""
    snapshot_ids = frozenset(snapshot.containers)
    current_ids = frozenset(live.containers)
    label_ids_by_name = {container.name.casefold(): container.id for container in labels.values()}
    applied_names = (provider._label_name(name).casefold() for name in connector_labels if name)
    removable = {
        label_id
        for name in applied_names
        if (label_id := label_ids_by_name.get(name)) is not None
        and label_id in current_ids
        and label_id not in snapshot_ids
    }
    body: dict[str, list[str]] = {}
    if INBOX_LABEL in snapshot_ids and INBOX_LABEL not in current_ids:
        body["addLabelIds"] = [INBOX_LABEL]
    if removable:
        body["removeLabelIds"] = sorted(removable)
    return body


def _gmail_restore_modify(
    provider: GmailProvider,
    internet_id: str,
    provider_id: str,
    body: dict[str, list[str]],
) -> None:
    """TEST SCAFFOLDING ONLY: mutate Gmail placement through its raw transport.

    This helper deliberately lives in ``reset.py`` and never becomes a
    ``MailProvider`` method. Moving a message back to Inbox exists only so local
    experiments can be repeated; shipped connector surfaces must not gain it.
    """
    with ProviderCall(GMAIL_PROVIDER_NAME, "reset_restore", message_id=internet_id):
        try:
            provider._api().users().messages().modify(
                userId="me",
                id=provider_id,
                body=body,
            ).execute()
        except HttpError as exc:
            if int(getattr(exc.resp, "status", 0)) == 404:
                raise _MessageMissingError(provider_id) from exc
            raise RestoreError(
                "Gmail restore modify failed; remaining placement mismatches were not hidden. "
                "ACTION REQUIRED: verify the credential and retry from the same snapshot."
            ) from exc


def _gmail_current_mailbox(provider: GmailProvider) -> CurrentMailbox:
    """Enumerate content-free Gmail placement, indexed by stable internet id."""
    current: CurrentMailbox = {}
    page_token: str | None = None
    while True:
        page = _gmail_page(provider, page_token)
        for reference in page.get("messages", []) or []:
            raw = _gmail_message(provider, str(reference.get("id", "")))
            _record_current(current, raw)
        page_token = _optional_token(page.get("nextPageToken"), "Gmail nextPageToken")
        if page_token is None:
            return current


def _record_current(current: CurrentMailbox, raw: dict[str, Any]) -> None:
    """Record one live row by internet id and reject ambiguous duplicates."""
    internet_id = _gmail_internet_id(raw)
    if not internet_id:
        raise RestoreError(
            "A listed Gmail message has no internet_message_id; no restore writes were attempted."
        )
    if internet_id in current:
        raise RestoreError(
            "Duplicate internet_message_id encountered; no further restore writes were attempted."
        )
    current[internet_id] = _CurrentMessage(
        provider_id=str(raw.get("id", "")),
        internet_message_id=internet_id,
        containers=tuple(str(item) for item in raw.get("labelIds", []) or []),
    )


def _placement_mismatches(
    snapshot: Snapshot,
    current: CurrentMailbox,
    missing: set[str],
) -> tuple[str, ...]:
    """Return stable ids whose surviving placement still differs from the snapshot."""
    return tuple(
        internet_id
        for internet_id, expected in sorted(snapshot.items())
        if internet_id not in missing
        and frozenset(current[internet_id].containers) != frozenset(expected.containers)
    )


def _log_missing(missing: set[str]) -> None:
    """Announce each missing snapshot row by stable id only."""
    for internet_id in sorted(missing):
        logger.warning(f"RESET: restore skipped missing internet_message_id={internet_id}")


def _enumerate(provider: MailProvider, scope: MailboxScope, limit: int) -> Snapshot:
    """Dispatch to the provider-specific native page loop."""
    if isinstance(provider, GmailProvider):
        return _gmail_snapshot(provider, scope, limit)
    if isinstance(provider, OutlookProvider):
        return _graph_snapshot(provider, scope, limit)
    raise SnapshotError(
        f"Snapshot does not support provider type {type(provider).__name__}; no file was written."
    )


def _container_index(provider: MailProvider) -> dict[str, Container]:
    """Fetch the provider's container vocabulary once for scope decisions."""
    return {container.id: container for container in provider.list_containers()}


def _covered(
    container_ids: tuple[str, ...],
    scope: MailboxScope,
    containers: dict[str, Container],
) -> bool:
    """Delegate the whole placement decision to ``MailboxScope.covers_all``.

    AN ID THE LISTING DID NOT CARRY REFUSES THE MESSAGE, and that is the same
    correction ``watch.round._covered`` carries (#198). The unknown id used to
    be passed to the scope AS A FOLDER NAME with no well-known tag, so the
    rules answered a question about a name no folder has -- and an
    ``all_except`` scope excluding "Client Matters" admitted a message sitting
    in exactly that folder whenever the listing lacked its id. A snapshot is a
    read of the mailbox, so admitting an unreadable placement is the same
    mistake here as it is in the watcher.

    A boolean is enough at THIS boundary, unlike the watcher's ``_Placement``:
    the one caller skips the message either way and keeps no per-outcome
    tally, so a reason it cannot report would be a reason nothing reads. It is
    logged instead, with counts only.

    Args:
        container_ids: The provider-native container ids on one message.
        scope: The installed scope. Consulted, never re-derived.
        containers: The listing this snapshot run read, by id.

    Returns:
        Whether the snapshot may record this placement.
    """
    known = [containers.get(identifier) for identifier in container_ids]
    if any(container is None for container in known):
        # Counts only: a line about mail we refused to read must not quote it.
        logger.debug(
            f"RESET: container not enumerated, message refused "
            f"message_containers={len(container_ids)} "
            f"unknown={sum(1 for container in known if container is None)} "
            "(run list-folders: the scope is not what refused this)"
        )
        return False
    names = [container.name for container in known if container is not None]
    tags = [container.well_known for container in known if container is not None]
    return scope.covers_all(names, well_known=tags)


def _record(
    snapshot: Snapshot,
    internet_id: str,
    container_ids: tuple[str, ...],
    limit: int,
) -> None:
    """Add one structurally safe row, refusing ambiguity and truncation."""
    if not internet_id:
        raise SnapshotError(
            "A listed message has no internet_message_id; no file was written because "
            "provider ids do not survive every move."
        )
    if not container_ids:
        raise SnapshotError(
            "A listed message has no containers; no file was written because its placement "
            "cannot be restored."
        )
    if internet_id in snapshot:
        raise SnapshotError(
            "Duplicate internet_message_id encountered; no file was written because two "
            "messages cannot share one snapshot key."
        )
    snapshot[internet_id] = SnapshotMessage(
        internet_message_id=internet_id,
        containers=container_ids,
    )
    if len(snapshot) > limit:
        raise SnapshotError(
            f"Snapshot limit {limit} exceeded; no file was written. "
            "Re-run with a larger --limit after confirming the mailbox scope."
        )


def _gmail_snapshot(provider: GmailProvider, scope: MailboxScope, limit: int) -> Snapshot:
    """Enumerate Gmail mailbox pages directly, then keep only covered placements."""
    containers = _container_index(provider)
    snapshot: Snapshot = {}
    page_token: str | None = None
    while True:
        page = _gmail_page(provider, page_token)
        for reference in page.get("messages", []) or []:
            raw = _gmail_message(provider, str(reference.get("id", "")))
            container_ids = tuple(str(item) for item in raw.get("labelIds", []) or [])
            if not _covered(container_ids, scope, containers):
                continue
            _record(snapshot, _gmail_internet_id(raw), container_ids, limit)
        page_token = _optional_token(page.get("nextPageToken"), "Gmail nextPageToken")
        if page_token is None:
            return snapshot


def _gmail_page(provider: GmailProvider, page_token: str | None) -> dict[str, Any]:
    """Read one native Gmail message-list page, including Spam and Trash for filtering."""
    request: dict[str, Any] = {
        "userId": "me",
        "maxResults": MAX_PAGE_SIZE,
        "includeSpamTrash": True,
    }
    if page_token is not None:
        request["pageToken"] = page_token
    with ProviderCall(GMAIL_PROVIDER_NAME, "reset_snapshot_list") as call:
        try:
            page = with_retry(
                call,
                lambda: provider._api().users().messages().list(**request).execute(),
            )
        except HttpError as exc:
            raise SnapshotError(
                "Gmail snapshot listing failed; no file was written. "
                "ACTION REQUIRED: verify the credential and retry."
            ) from exc
    return dict(page)


def _gmail_message(provider: GmailProvider, message_id: str) -> dict[str, Any]:
    """Fetch only the stable key and provider placement for one Gmail message."""
    with ProviderCall(GMAIL_PROVIDER_NAME, "reset_snapshot_message") as call:
        try:
            raw = with_retry(
                call,
                lambda: (
                    provider
                    ._api()
                    .users()
                    .messages()
                    .get(
                        userId="me",
                        id=message_id,
                        format="metadata",
                        metadataHeaders=list(GMAIL_SNAPSHOT_HEADERS),
                    )
                    .execute()
                ),
            )
        except HttpError as exc:
            raise SnapshotError(
                "Gmail snapshot message read failed; no file was written. "
                "ACTION REQUIRED: verify the credential and retry."
            ) from exc
    return dict(raw)


def _gmail_internet_id(raw: dict[str, Any]) -> str:
    """Extract Message-Id from the one-header Gmail metadata projection."""
    payload = raw.get("payload", {}) or {}
    headers = payload.get("headers", []) or []
    values = {
        str(header.get("name", "")).casefold(): str(header.get("value", "")) for header in headers
    }
    return values.get("message-id", "")


def _graph_snapshot(provider: OutlookProvider, scope: MailboxScope, limit: int) -> Snapshot:
    """Enumerate every in-scope Graph folder with an explicit next-link loop."""
    containers = _container_index(provider)
    snapshot: Snapshot = {}
    for container in containers.values():
        if not scope.covers(container.name, well_known=container.well_known):
            continue
        _graph_folder_snapshot(provider, container.id, limit, snapshot)
    return snapshot


def _graph_folder_snapshot(
    provider: OutlookProvider,
    folder_id: str,
    limit: int,
    snapshot: Snapshot,
) -> None:
    """Read every message page in one Graph folder into the shared snapshot."""
    params = _graph_page_params()
    while True:
        page = provider._get(
            "me",
            "mailFolders",
            folder_id,
            "messages",
            params=params,
            operation="reset_snapshot_list",
        )
        for raw in page.get("value", []) or []:
            container_ids = (str(raw.get("parentFolderId", "")),)
            if container_ids != (folder_id,):
                raise SnapshotError(
                    "Graph returned a message outside the folder being enumerated; "
                    "no file was written."
                )
            _record(snapshot, str(raw.get("internetMessageId", "")), container_ids, limit)
        next_params = _graph_next_params(page.get("@odata.nextLink"))
        if next_params is None:
            return
        params = next_params


def _graph_page_params() -> dict[str, Any]:
    """The fixed content-free Graph projection for every page."""
    return {"$top": MAX_PAGE_SIZE, "$select": GRAPH_SNAPSHOT_FIELDS}


def _graph_next_params(next_link: object) -> dict[str, Any] | None:
    """Carry only Graph's opaque page cursor into the same validated endpoint."""
    cursor = graph_cursor(next_link)
    return None if cursor is None else {**_graph_page_params(), **cursor}


def graph_cursor(next_link: object) -> dict[str, Any] | None:
    """Extract Graph's opaque page cursor, or ``None`` at the last page.

    Separated from ``_graph_next_params`` so the restore path in ``reset_graph``
    can reuse the validation without inheriting the snapshot's ``$select``: the
    two walks project different field sets, and a shared cursor reader is the
    only part they genuinely have in common.
    """
    if next_link is None:
        return None
    if not isinstance(next_link, str) or not next_link:
        raise SnapshotError("Graph returned an invalid @odata.nextLink; no file was written.")
    query = parse_qs(urlsplit(next_link).query)
    cursor = {key: values[-1] for key in GRAPH_CURSOR_KEYS if (values := query.get(key))}
    if not cursor:
        raise SnapshotError(
            "Graph returned @odata.nextLink without a supported page cursor; no file was written."
        )
    return cursor


def _optional_token(value: object, label: str) -> str | None:
    """Validate an optional provider cursor without stringifying malformed data."""
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise SnapshotError(f"{label} is invalid; no file was written.")
