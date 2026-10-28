"""Models - the pure data shapes that more than one package agrees on.

Rung 4 of the promotion ladder (``utils/__init__`` states it): a helper used
across packages lands in root ``utils``, and a pure DATA SHAPE used across
packages lands here. Pydantic only, no behaviour -- nothing in this package
performs I/O, calls a provider, or reads configuration, and it imports nothing
from the rest of ``inbox_zero_connector``. That directional rule is the whole
point: ``providers.base`` used to own ``Message`` and ``Container``, which
made every consumer outside ``providers`` import the provider interface just to
name a mail header, and two of them (``config``,
``classifiers.openai_compatible``) had to hide that import under
``TYPE_CHECKING`` to break the resulting cycle. A package with no inbound
dependencies cannot form one.

What belongs here is a shape imported by TWO OR MORE packages. What does not:

- ``Thread``, ``SentMessage``, ``SentSelection``, ``DraftResult``, and
  ``ChangeSet`` stay in ``providers.base``. Every consumer of each is inside
  ``providers``, so promoting them would move a shape above its actual usage --
  the ladder's standing prohibition.
- The Graph wire shapes stay in ``providers.outlook.models``. They are a
  VENDOR SCHEMA -- their fields are Microsoft's to change -- and a vendor schema
  is never promoted to a shared shape, because then a provider's wire format
  becomes the whole package's contract.
- ``ProviderError`` stays in ``providers.base``. It is an exception, not a
  shape, and it names a provider-boundary failure.

Key Components:
    - mail: ``ProviderModel``, the frozen ``extra="forbid"`` base; ``Message``,
      one deliberately thin mail header; ``Container``, one provider-side place
      a message can be (a Graph folder, a Gmail label) with the ``exclusive``
      flag that writes down the structural difference between the two.

Pattern/Convention:
    Import from the OWNING MODULE, not from this ``__init__``::

        from inbox_zero_connector.models.mail import Message

    There are deliberately no re-exports here, which is the one place this
    package differs from ``utils``. ``utils`` re-exports because it is a flat
    floor of unrelated helpers; ``models`` expects to grow one module per
    subject area, and a flat namespace over several subjects makes the owning
    module invisible at the call site.

    Add a shape here only when a SECOND package needs it. One package's shape
    lives with that package.

Example:
    >>> from inbox_zero_connector.models.mail import Message
    >>> msg = Message(
    ...     id="AAMkAGI2",
    ...     thread_id="AAQkAGI2",
    ...     sender="ap@acme.example",
    ...     subject="Invoice 4417 past due",
    ...     received="2026-08-20T14:02:11Z",
    ... )
    >>> msg.is_unread
    False

See Also:
    - inbox_zero_connector.providers.base: the provider interface, which
      re-exports these three so existing provider-side callers are unchanged,
      and which owns the shapes whose consumers are all inside ``providers``.
    - inbox_zero_connector.utils.sections: the same directional rule applied to
      the settings base -- pulled down to a floor so two modules that both need
      it do not have to import each other.
"""

from __future__ import annotations
