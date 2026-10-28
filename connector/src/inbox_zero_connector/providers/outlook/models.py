"""Typed Graph response shapes. Pydantic replaces hand-walked dicts.

This module is the as-decided half of the Graph hardening recorded in
``SPEC.md`` (2026-08-17): borrow the SDK's modeling approach -- typed
response objects -- without taking its dependency tree. Every field here is
one this adapter actually consumes; Graph's hundreds of other properties are
ignored by ``extra="ignore"`` rather than modeled.

WHY MODELS INSTEAD OF ``.get()`` CHAINS
    ``((raw.get("from") or {}).get("emailAddress") or {}).get("address", "")``
    is four decisions about absent data made inline, per call site, and a
    response whose ``body`` arrives as a STRING instead of an object raised
    ``AttributeError`` -- an exception outside the adapter's contract, so the
    tool layer showed the host AI a crash instead of a refusal. A model makes
    each of those decisions once, in a declaration, and turns a shape this
    connector does not accept into ONE well-formed refusal at the parse
    boundary (see ``parse_graph``).

WHAT THE MODELS DELIBERATELY DO NOT DO
    Defaults here mirror the adapter's existing lenient reads -- an absent
    ``subject`` is ``""``, an absent ``id`` is ``""`` -- because the 489-test
    suite is the specification for this rework and refusal decisions belong
    to the call sites that already own them. The one absence that carries
    meaning, ``parentFolderId``, stays ``None`` so ``resolved_or_none`` keeps
    refusing it as evidence; the sent-only contract check is unchanged.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inbox_zero_connector.providers.base import ProviderError


class _GraphShape(BaseModel):
    """Common config: alias-populated, unknown Graph properties ignored."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)


class GraphEmailAddress(_GraphShape):
    """The inner ``emailAddress`` object on a sender or recipient."""

    address: str = ""


class GraphRecipient(_GraphShape):
    """One entry of ``toRecipients``/``ccRecipients``/``from``."""

    email_address: GraphEmailAddress = Field(GraphEmailAddress(), alias="emailAddress")


class GraphBody(_GraphShape):
    """The ``body`` object. Its ``content`` is mailbox CONTENT -- never logged."""

    content: str = ""


class GraphFlag(_GraphShape):
    """The ``flag`` object; only ``flagStatus`` is consumed."""

    flag_status: str = Field("", alias="flagStatus")


class GraphMessage(_GraphShape):
    """One message, across every projection this adapter requests.

    A single model rather than one per ``$select`` list: a field a projection
    did not ask for simply takes its default, which is the same reading the
    ``.get()`` chains gave an absent key.
    """

    id: str = ""
    conversation_id: str = Field("", alias="conversationId")
    sender: GraphRecipient | None = Field(None, alias="from")
    subject: str = ""
    received: str = Field("", alias="receivedDateTime")
    sent: str = Field("", alias="sentDateTime")
    body_preview: str = Field("", alias="bodyPreview")
    is_read: bool = Field(False, alias="isRead")
    importance: str = ""
    flag: GraphFlag = GraphFlag(flagStatus="")
    body: GraphBody = GraphBody()
    to_recipients: tuple[GraphRecipient, ...] = Field((), alias="toRecipients")
    cc_recipients: tuple[GraphRecipient, ...] = Field((), alias="ccRecipients")
    categories: tuple[str, ...] = ()
    # None, not "": absence must stay distinguishable from a value so that
    # resolved_or_none can refuse it as evidence on the sent-only path.
    parent_folder_id: str | None = Field(None, alias="parentFolderId")

    def recipient_addresses(self) -> list[str]:
        """To + Cc addresses, de-duplicated, order preserved, blanks dropped."""
        seen: dict[str, None] = {}
        for recipient in (*self.to_recipients, *self.cc_recipients):
            if recipient.email_address.address:
                seen.setdefault(recipient.email_address.address, None)
        return list(seen)

    @property
    def sender_address(self) -> str:
        """The sender's address, or ``""`` when Graph supplied none."""
        return self.sender.email_address.address if self.sender else ""


class GraphMessageList(_GraphShape):
    """A collection response: ``{"value": [message, ...]}``."""

    value: tuple[GraphMessage, ...] = ()


class GraphFolder(_GraphShape):
    """One mail folder; only ``id`` and ``displayName`` are consumed."""

    id: str = ""
    display_name: str = Field("", alias="displayName")


class GraphFolderList(_GraphShape):
    """A folder collection response."""

    value: tuple[GraphFolder, ...] = ()


def parse_graph[ModelT: BaseModel](
    model: type[ModelT], data: dict[str, Any], *, operation: str
) -> ModelT:
    """Validate one Graph response body into its model, or refuse.

    THIS IS THE SHAPE BOUNDARY, the response-side sibling of ``_graph_path``:
    everything the adapter consumes from Graph passes through here exactly
    once, so "this response is not a shape we accept" is one refusal in one
    place instead of an ``AttributeError`` somewhere down a ``.get()`` chain.

    The refusal carries the operation, the problem count, and the FIELD PATHS
    only. Field paths are this package's own vocabulary (they name aliases
    declared above); the offending VALUES are server-controlled and may embed
    mailbox content, so they never reach the message, the log line, or the
    host AI's context.

    Args:
        model: The expected shape.
        data: The response body, already unwrapped from HTTP by the caller.
        operation: The adapter operation, for the refusal and the log line.

    Returns:
        The validated model.

    Raises:
        ProviderError: When the body does not fit the declared shape.
    """
    try:
        parsed = model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        joined = ", ".join(fields[:MAX_REPORTED_FIELDS])
        logger.error(
            f"GRAPH: response shape refused operation={operation} "
            f"model={model.__name__} problem_count={exc.error_count()} fields={joined}"
        )
        raise ProviderError(
            f"Microsoft Graph returned a response this connector does not accept "
            f"for {operation} (fields: {joined}). The response was refused, not "
            f"partially read. ACTION REQUIRED: retry; if this repeats, the "
            f"mailbox or a proxy is returning a non-Graph shape."
        ) from exc
    logger.debug(f"GRAPH: response shape ok operation={operation} model={model.__name__}")
    return parsed


MAX_REPORTED_FIELDS = 5
"""Field paths named in one shape refusal. Enough to diagnose, bounded on
principle: paths are our vocabulary, but their COUNT is server-controlled."""
