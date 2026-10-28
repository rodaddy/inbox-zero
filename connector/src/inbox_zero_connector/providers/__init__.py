"""Mailbox provider adapters.

Every adapter implements ``base.MailProvider`` and nothing wider. The
interface is the security boundary: see ``base`` for what is deliberately
absent from it -- no send, no delete, no forward, and no recipient parameter
on ``create_draft``.

Key Components:
    - base: the ABC and the value objects. THE security boundary.
    - factory: ``get_provider``, the one place an adapter is constructed.
    - gmail: the Google adapter package (``provider``, ``ids``, ``query``,
      ``messages``, ``history``).
    - outlook: the Microsoft Graph adapter package (``provider``, ``models``,
      ``evidence``, ``errors``, ``categories``, ``delta``, ``reauth``).
    - limits: ``MAX_PAGE_SIZE`` and ``cap_page``, the one page bound both
      adapters clamp to.
    - ids: ``validate_id`` and ``MAX_ID_CHARS``, the three-arm no-echo
      refusal every caller-supplied id passes; each adapter keeps its own
      grammar and its own tail.
    - dates: ``parse_iso_date``, the one date-bound parse both adapters
      refuse through; each formats the parsed date for its own syntax.
    - retry: the bounded retry policy every outbound read runs under.
    - containers: the shared well-known folder vocabulary both adapters
      answer ``list_containers`` in, held side by side so it cannot drift.
    - granted: reads the GRANTED scope list off a live credential, one
      reader per vendor, for ``services.auth.grants`` to interpret.

Pattern/Convention:
    Construct through ``get_provider``, never by importing an adapter class
    directly -- the factory is what applies the installed drafts posture. Add
    a provider by adding a package and one row to the factory; adding a
    CAPABILITY means changing ``base``, which is a security-posture change and
    requires revisiting ``scopes``, the README enforcement table, and every
    buyer-facing claim.

    Promotion ladder inside this package: a helper used by one adapter stays
    in that adapter's package; a mechanism both adapters carry (``retry``,
    ``containers``, ``granted`` today) lives flat at ``providers/``; only a
    domain-neutral primitive goes further up to ``utils``. Duplication
    triggers the promotion, never a second copy.

Example:
    >>> from inbox_zero_connector.providers import get_provider
    >>> provider = get_provider("google", drafts_enabled=False)

See Also:
    - inbox_zero_connector.services.auth.scopes: the OAuth security manifest
    - inbox_zero_connector.providers.base: the boundary and its absences
"""

from __future__ import annotations

from inbox_zero_connector.providers.base import (
    AuthError as AuthError,
)
from inbox_zero_connector.providers.base import (
    DraftResult as DraftResult,
)
from inbox_zero_connector.providers.base import (
    MailProvider as MailProvider,
)
from inbox_zero_connector.providers.base import (
    Message as Message,
)
from inbox_zero_connector.providers.base import (
    ProviderError as ProviderError,
)
from inbox_zero_connector.providers.base import (
    SentMessage as SentMessage,
)
from inbox_zero_connector.providers.base import (
    SentSelection as SentSelection,
)
from inbox_zero_connector.providers.base import (
    Thread as Thread,
)
from inbox_zero_connector.providers.factory import (
    get_provider as get_provider,
)
