"""Gmail wire codec - decode API payloads into models, encode replies back.

Module-level pure functions with no client, no credential, and no I/O: every
input is a dict the Gmail API already returned (or header values read from
one), so this is the one place payload shape knowledge lives. ``provider``
owns the calls; this module owns what the bytes mean.

Key Components:
    - _to_message / _to_sent / _headers / _extract_body: API payload -> model.
    - _participants / _thread_labels / _without_owner: thread-level rollups.
    - _reply_headers / _sanitize_header / _encode_reply: build the outbound
      reply MIME, with every header folded to one line so a hostile subject
      cannot smuggle a header of its own.
    - _encode_self_draft: the recipientless digest draft MIME (#65). Sets no
      To/Cc/Bcc and takes no parameter that could.

Pattern/Convention:
    Functions are private-named because ``provider`` is their only production
    caller; tests import them directly to pin behavior. Nothing here may
    import the API client - if a function needs the network it does not
    belong in this module.

Example:
    >>> from inbox_zero_connector.providers.gmail.messages import _sanitize_header
    >>> _sanitize_header("a\nb")
    'a b'

See Also:
    - inbox_zero_connector.providers.gmail.provider: the caller.
    - inbox_zero_connector.providers.base: the model types built here.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Final

from inbox_zero_connector.providers.base import Message, ProviderError, SentMessage
from inbox_zero_connector.providers.gmail.ids import bare as _bare
from inbox_zero_connector.providers.gmail.ids import split_addresses as _split_addresses
from inbox_zero_connector.providers.gmail.ids import unique as _unique

IMPORTANT_LABEL = "IMPORTANT"
"""Gmail's own importance label; owned here because decoding reads it."""

GMAIL_FLAG_LABELS: Final[frozenset[str]] = frozenset({"UNREAD", "STARRED", "IMPORTANT"})
"""Gmail labels that are FLAGS on a message, never PLACES it sits in.

Gmail models state as labels, so its wire format spells "this message is
unread" the same way it spells "this message is in the inbox". The rest of
this connector does not: ``Message`` carries unread and important as their own
boolean fields, and ``containers`` means placement and only placement.
Deciding which is which is the adapter's job, because the wire format is the
only thing that conflates them.

THIS IS THE FIX FOR A LIVE DEFECT, head-verified against a real Gmail mailbox
on 2026-08-22. ``MailboxScope.covers_all`` is any-exclusion-wins -- one
uncovered entry refuses the whole message -- and under the default
``inbox_only`` mode nothing matches ``UNREAD``, which has no well-known tag
and is not a folder. Reporting it as a container made the watcher refuse EVERY
unread inbox message, which is every message it exists to read. Nothing was
lost by the filter: ``is_unread`` and ``is_important`` are computed from the
UNFILTERED label list a few lines below, so the facts survive in the fields
that mean them.
"""


CATEGORY_PREFIX: Final = "CATEGORY_"
"""Prefix of Gmail's inbox-tab labels -- ``CATEGORY_PERSONAL``, ``_UPDATES``,
``_PROMOTIONS``, ``_SOCIAL``, ``_FORUMS``.

A prefix test rather than an enumerated set, because Gmail has added category
labels before and the set that must be recognised is "whatever Gmail tabs
exist", not "the ones known on the day this was written".
"""


def _is_category(label: str) -> bool:
    """Is this label one of Gmail's inbox-tab categories?

    THE SINGLE DEFINITION, used by ``_is_place`` to reject these from
    ``containers`` and by ``_to_message`` to collect them into ``categories``.
    One function rather than the same prefix test written twice, because the
    two answers are complements: a second copy could drift and leave a label
    both a place and a category, or neither, which is how the category rule in
    ``services.triage.decide`` silently lost its input once already.

    Args:
        label: One raw Gmail label id off the message's ``labelIds``.

    Returns:
        True when the label is a Gmail category tag.
    """
    return label.startswith(CATEGORY_PREFIX)


def _is_place(label: str) -> bool:
    """Is this label somewhere the message SITS, rather than something it IS?

    Two rejections, one question. ``GMAIL_FLAG_LABELS`` names the three
    explicit flags; ``_is_category`` covers Gmail's inbox tabs, which are
    Gmail's own verdict ABOUT a message and not a second place it lives -- a
    tabbed message is still in the inbox, and ``INBOX`` is already there saying
    so. Neither rejection loses a fact: the flags survive as ``is_unread`` and
    ``is_important``, the categories as ``Message.categories``.

    Everything else -- ``INBOX``, ``SENT``, ``DRAFT``, ``SPAM``, ``TRASH``, and
    every user ``Label_<n>`` -- is a place and passes through.

    Args:
        label: One raw Gmail label id off the message's ``labelIds``.

    Returns:
        True when the label names a place and belongs in ``containers``.
    """
    return label not in GMAIL_FLAG_LABELS and not _is_category(label)


def _encode_reply(
    to_addresses: list[str], cc_addresses: list[str], headers: dict[str, str], body: str
) -> str:
    """Assemble the reply MIME message and base64url-encode it for Gmail.

    Every address here came from ``_reply_headers``, which reads the thread's
    own headers. Nothing a caller supplied reaches this function.

    Args:
        to_addresses: Reply-To (or From) of the last message on the thread.
        cc_addresses: Cc carried forward so a reply-all thread stays intact.
        headers: Subject and the In-Reply-To/References threading pair.
        body: The reply text the AI wrote.

    Returns:
        The base64url-encoded message, as Gmail's raw field wants it.

    Raises:
        ProviderError: The headers could not be assembled into a valid message.
            ``_reply_headers`` already sanitizes the copied values, so reaching
            this is a second line of defence -- but the failure mode it covers
            is a ValueError out of the email policy, which no caller on the
            create_draft path catches, and an uncaught one there takes the
            whole tool down rather than declining one draft.
    """
    try:
        mime = EmailMessage()
        mime["To"] = ", ".join(to_addresses)
        if cc_addresses:
            mime["Cc"] = ", ".join(cc_addresses)
        for name, value in headers.items():
            mime[name] = value
        mime.set_content(body)
        return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    except ValueError as exc:
        raise ProviderError(
            f"Gmail create_draft could not assemble a valid reply message: {exc}. This "
            "usually means a header copied from the inbound message is malformed. No "
            "draft was created."
        ) from exc


def _encode_self_draft(subject: str, body: str) -> str:
    """Assemble a recipientless draft and base64url-encode it for Gmail.

    NO ``To``, NO ``Cc``, NO ``Bcc`` is set here, and there is no parameter
    that could set one. Gmail files the result in the account's own Drafts;
    the owner reads it there. This is the digest delivery encoder (#65).

    Args:
        subject: The draft's subject line, folded to one line so it cannot
            smuggle a header of its own.
        body: The draft's plain-text body.

    Returns:
        The base64url-encoded message, as Gmail's raw field wants it.

    Raises:
        ProviderError: The message could not be assembled. Same second line of
            defence ``_encode_reply`` carries, for the same reason: a ValueError
            out of the email policy has no handler on the calling path.
    """
    try:
        mime = EmailMessage()
        mime["Subject"] = _sanitize_header(subject)
        mime.set_content(body)
        return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    except ValueError as exc:
        raise ProviderError(
            f"Gmail create_self_draft could not assemble a valid message: {exc}. "
            "No draft was created."
        ) from exc


def _participants(items: list[dict[str, Any]]) -> list[str]:
    """Every address on the thread: From plus To plus Cc, de-duplicated.

    The owner is INCLUDED. Removing them is the tool layer's job, done once
    against the authenticated account, so the "minus the owner" rule lives in
    one place rather than once per adapter.

    ONE ``_split_addresses`` CALL PER HEADER, and that is load-bearing rather
    than stylistic. A previous revision joined every header on the thread into
    one string and parsed once, to save lines against the file's size ceiling.
    ``getaddresses`` fails for the whole input it is handed and never per
    token, so that made ONE unterminated construct on ONE message erase every
    address on the conversation -- ``participants`` came back empty, and
    ``create_draft`` then refused the thread for having nobody to reply to.
    Per-header keeps the blast radius at the header that is actually broken.
    """
    return _unique(
        address
        for item in items
        for key in ("from", "to", "cc")
        for address in _split_addresses(_headers(item).get(key, ""))
    )


def _thread_labels(items: list[dict[str, Any]]) -> list[str]:
    """Union of Gmail label ids across the thread. Feeds the sales-voice rule."""
    labels = (item.get("labelIds", []) or [] for item in items)
    return _unique(x for group in labels for x in group if isinstance(x, str))


def _to_sent(raw: dict[str, Any]) -> SentMessage:
    """Decode a full messages.get payload into a SentMessage.

    Recipients are To PLUS Cc, concatenated rather than de-duplicated: this is
    a record of who the owner actually addressed, and the voice work downstream
    reads it as an audience count. The body is extracted here (unlike
    ``_to_message``, which carries only a snippet) because sent mail is sampled
    precisely to learn how the owner writes.
    """
    headers = _headers(raw)
    recipients = _split_addresses(headers.get("to", "")) + _split_addresses(headers.get("cc", ""))
    return SentMessage(
        id=str(raw.get("id", "")),
        thread_id=str(raw.get("threadId", "")),
        recipients=recipients,
        subject=headers.get("subject", ""),
        sent=headers.get("date", ""),
        body=_extract_body(raw.get("payload", {}) or {}),
    )


def _without_owner(addresses: list[str], owner: str) -> list[str]:
    """Drop the owner from an address list, comparing on the bare address.

    An empty ``owner`` means it could not be resolved, so nothing is dropped.
    """
    return [a for a in addresses if _bare(a) != owner] if owner else addresses


def _sanitize_header(value: str) -> str:
    """Flatten a copied header value to one safe line.

    Subject and References are copied forward from the INBOUND message, which
    means their content is attacker-controlled whenever the inbound message is.
    A CR or LF in one of them is a header-injection attempt, and Python's email
    policy answers it with ValueError -- a type nothing on the create_draft
    path catches, so a single hostile message would otherwise break drafting on
    that thread for good.

    Folding whitespace is collapsed to single spaces rather than refused: a
    legitimately long References chain arrives already folded across lines, so
    refusing every newline would reject ordinary mail. What matters is that no
    line break survives into the header we emit.

    ``str.split()`` with no argument, not ``split("\\n")``: the no-argument form
    collapses RUNS of whitespace, so a CRLF pair yields one space rather than
    the two an explicit newline split leaves behind.
    """
    return " ".join(value.split())


def _reply_headers(
    last: dict[str, Any], owner: str = ""
) -> tuple[list[str], list[str], dict[str, str]]:
    """Derive a reply's To, Cc, and threading headers from the last message.

    THE ONLY SOURCE OF RECIPIENTS. Reply-To wins over From when present, which
    is what a mail client does. Cc is carried forward so a reply-all-shaped
    thread stays intact; nothing new is ever added.

    ``owner`` is dropped from both lists when known. It is dropped HERE, and
    not only at the tool layer, because Gmail has no server-side reply action:
    these lists become the draft's literal To and Cc headers, so the tool
    layer's own owner-stripped recipient list would otherwise be a report about
    a message that said something else. The header is what the owner opens and
    what would be sent; it is the copy that has to be right. An empty ``owner``
    means the address could not be resolved, and nothing is dropped.

    Subject and References are passed through ``_sanitize_header`` because both
    are copied from inbound, attacker-influenced text.
    """
    headers = _headers(last)
    # Every candidate arm is owner-stripped FIRST, then the first arm that
    # still has somebody in it wins. Resolving the Reply-To-over-From
    # preference before stripping is what made a self-addressed Reply-To (an
    # ordinary thing: a list, a desk alias the owner also owns, their own last
    # reply) collapse the To line to empty and refuse a thread whose From held
    # a real correspondent. Cc is last: promoting it beats a bare To header,
    # and if it is empty too the caller refuses the draft.
    arms = [
        _without_owner(_split_addresses(headers.get(key, "")), owner)
        for key in ("reply-to", "from", "cc")
    ]
    won = next((index for index, arm in enumerate(arms) if arm), 2)
    # The Cc is carried forward only when it did not become the To itself.
    to_addresses, cc_addresses = arms[won], [] if won == 2 else arms[2]

    threading: dict[str, str] = {}
    subject = _sanitize_header(headers.get("subject", ""))
    threading["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    message_id = _sanitize_header(headers.get("message-id", ""))
    if message_id:
        threading["In-Reply-To"] = message_id
        references = _sanitize_header(headers.get("references", ""))
        threading["References"] = f"{references} {message_id}".strip()
    return to_addresses, cc_addresses, threading


def _headers(raw: dict[str, Any]) -> dict[str, str]:
    """Flatten Gmail's ``payload.headers`` list into a lookup dict.

    Names are LOWERCASED because header names are case-insensitive on the wire
    and Gmail is not consistent about them ("Message-Id" and "Message-ID" both
    occur). Every caller in this module therefore looks up in lower case and
    none of them has to guess which spelling arrived.

    A repeated header keeps the LAST occurrence, which is the dict-comprehension
    default and is fine for the addressing and threading headers read here; no
    caller needs the multi-value form.
    """
    payload = raw.get("payload", {}) or {}
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in payload.get("headers", []) or []
    }


def _to_message(raw: dict[str, Any]) -> Message:
    """Decode a message payload into the cross-provider ``Message`` model.

    Works on BOTH shapes Gmail returns: the ``format="metadata"`` header
    projection ``list_inbox`` fetches and the ``format="full"`` payload
    ``get_thread`` fetches. Every field is read defensively out of the dict, so
    the narrower projection simply yields empty strings rather than a KeyError,
    and there is one decoder instead of one per fetch format.

    Unread and important are derived from Gmail's own label ids rather than
    from any dedicated flag field, because Gmail has none -- state IS labels
    here. See the inline notes for why ``sender`` is the bare address, and
    ``GMAIL_FLAG_LABELS`` for why ``containers`` carries every PLACE label and
    no flag. The filter narrows placement without dropping a fact: unread and
    important are read from the unfiltered list, and the ``CATEGORY_*`` tabs
    land in ``categories``.
    """
    headers = _headers(raw)
    labels = raw.get("labelIds", []) or []
    # BARE address, not the raw header. The raw form ("Name <addr>") leaked a
    # display name and an angle bracket into `sender` -- caught on the FIRST
    # live mailbox run (2026-08-18), where one real From header shape parsed
    # and another did not. Outlook's adapter already returns the bare address;
    # the two providers must agree on the shape of the same tool field.
    senders = _split_addresses(headers.get("from", ""))
    return Message(
        id=str(raw.get("id", "")),
        thread_id=str(raw.get("threadId", "")),
        sender=senders[0] if senders else "",
        subject=headers.get("subject", ""),
        received=headers.get("date", ""),
        internet_message_id=headers.get("message-id", ""),
        snippet=str(raw.get("snippet", "")),
        is_unread="UNREAD" in labels,
        is_important=IMPORTANT_LABEL in labels,
        # ALL PLACE labels. A Gmail message routinely holds several at once
        # (Container.exclusive is False for Gmail), so picking one would be the
        # adapter inventing a placement the provider never stated -- but a FLAG
        # is not a placement at all, and `_is_place` is what tells them apart.
        containers=tuple(str(label) for label in labels if _is_place(str(label))),
        # The tab labels `_is_place` just rejected, kept as what they actually
        # are. Sorted because Gmail does not promise an order on `labelIds` and
        # this tuple is asserted against; the rule layer matches by exact id,
        # so order carries no meaning of its own.
        categories=tuple(sorted(str(label) for label in labels if _is_category(str(label)))),
    )


def _extract_body(payload: dict[str, Any]) -> str:
    """Pull the text/plain body, falling back to the top-level data blob."""

    def decode(data: str) -> str:
        """base64url-decode one body blob, replacing undecodable bytes."""
        return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")

    def own_data() -> str | None:
        """This part's own ``body.data``, or None when it carries no bytes."""
        return (payload.get("body", {}) or {}).get("data")

    # Guard clauses, not nesting: each line answers one question and leaves.
    # The plain-text arm and the trailing fallback read the SAME field, so it
    # is named once here rather than spelled out twice.
    if payload.get("mimeType") == "text/plain" and own_data():
        return decode(own_data() or "")
    for part in payload.get("parts", []) or []:
        found = _extract_body(part)
        if found:
            return found
    data = own_data()
    return decode(data) if data else ""
