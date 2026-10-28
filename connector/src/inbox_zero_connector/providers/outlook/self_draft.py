"""The Graph digest-draft write: a draft addressed to NOBODY (#65).

A module of its own rather than a method body in ``provider.py`` for the same
reason ``delta`` and ``categories`` are: that file sits at the package's
500-code-line ceiling, and an operation added inline would breach it. The
adapter keeps the interface method and hands this its own ``_post``, so the URL
boundary (``_graph_path``), the retry policy, and the ``ProviderCall`` logging
all still live where they did.

Separate from ``models`` on purpose: that module holds SHAPES and must not make
calls. This one makes exactly one.

NAMED ``self_draft``, NOT ``drafts``, AND THAT IS LOAD-BEARING. The no-send
done-means check (``_graph/done-means/inbox-zero-no-send.sh``, C5) scans for the
literal ``drafts.create``, because that is how a Gmail send would be spelled if
one were ever added. A module named ``drafts`` makes ``drafts.create_self_draft``
match it -- a false positive, and the correct answer to a strict scanner is a
different name here, never a loosened pattern there.
"""

from __future__ import annotations

from typing import Any, Protocol

from inbox_zero_connector.providers.outlook.models import GraphMessage, parse_graph

SELF_DRAFT_OP = "create_self_draft"
"""The operation label for the digest-draft write, named once.

The same string is the ``ProviderCall`` operation ``_post`` opens and the
``parse_graph`` operation below, and the two must agree: an operator
correlating a shape refusal against the call that produced it matches on this
word.
"""


class GraphPoster(Protocol):
    """The one thing ``create_self_draft`` needs from the adapter: its POST.

    A Protocol rather than importing ``OutlookProvider``, which would be a
    circular import -- ``provider`` imports this module. Passing the bound
    ``_post`` keeps the URL boundary (``_graph_path``), the retry policy, and
    the ``ProviderCall`` logging exactly where they already live.
    """

    def __call__(
        self, *segments: str, payload: dict[str, Any], operation: str = ...
    ) -> dict[str, Any]:
        """POST ``payload`` to the Graph path built from ``segments``; return the body.

        The signature is the adapter's bound ``_post`` verbatim, so the provider
        can be handed in with no wrapper. ``operation`` carries a default so
        callers indifferent to the audit label stay short, while
        ``create_self_draft`` passes ``SELF_DRAFT_OP`` and keeps the call
        correlatable in the logs.
        """
        ...


def create_self_draft(post: GraphPoster, subject: str, body: str) -> str:
    """Create a Graph draft addressed to NOBODY and return its id (#65).

    THE ABSENCE IS THE POINT, so it lives in one reviewable function rather
    than inline in an adapter method. There is no ``toRecipients``,
    ``ccRecipients``, or ``bccRecipients`` key in the payload and no parameter
    that could add one; Graph defaults each of those collections to empty, so
    the draft the owner opens in their own Drafts folder is addressed to no
    one.

    It lives HERE rather than in ``provider.py`` because that file is at the
    package's 500-code-line ceiling; ``delta`` and ``categories`` were split
    out for the same reason. The adapter keeps the interface method and hands
    this its own ``_post``, so every boundary that file owns still applies.

    ``POST /me/messages`` is the CREATE half of Graph's compose flow. The SEND
    half -- ``/send`` on the created message, and ``/me/sendMail`` -- is a
    separate action this package calls nowhere and the token does not carry
    ``Mail.Send`` for. Both path segments are ``_STATIC_SEGMENTS`` words, so
    nothing dynamic reaches the URL.

    Args:
        post: The adapter's bound ``_post``.
        subject: The draft's subject line.
        body: The draft's plain-text body.

    Returns:
        The provider's id for the created draft.
    """
    payload = {"subject": subject, "body": {"contentType": "Text", "content": body}}
    data = post("me", "messages", payload=payload, operation=SELF_DRAFT_OP)
    return parse_graph(GraphMessage, data, operation=SELF_DRAFT_OP).id
