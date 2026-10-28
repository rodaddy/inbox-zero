"""The audit spine - an append-only ledger of everything the connector did.

Every mailbox mutation, correction, and draft leaves a row here, which is
what makes the watcher trustworthy unattended: the morning question "what
did it do overnight" has a file-backed answer, and reset can put mail back
because the ledger says where it went. The decision trace sits beside the
ledger as a diagnostic companion: one content-free row per classified
message, recording what was seen, decided, and done, without ever standing in
for a receipt.

Key Components:
    - ledger: the append-only JSONL ledger - write a receipt, read them
      back, never rewrite history. ``LedgerEntry.schema_version`` serialises
      to the on-disk key ``schema`` via an alias; the wire key is load-bearing
      on every v1 row, so see the class docstring before changing either name.
      Since schema 5 a row also names the ``account`` it happened in, because
      one install now watches every configured mailbox and writes all of them
      into this one file; a row predating the field reads back ``None``, which
      means unknown and never "the first account".
    - trace: the append-only decision trace, one row per message the watch
      round classified. Content-free by construction -- sender DOMAIN only,
      reason capped -- and never a substitute for a ledger receipt.
    - undo: reverse one watch run's filings, planned from the RECEIPTS. It
      reads ``run_id`` and ``source_container`` (schema 4) rather than the
      trace, which is what lets a reversal move mail without guessing where
      each message came from; a row lacking a source is refused, never
      guessed at.

Pattern/Convention:
    Rows are appended, never edited; a wrong row is corrected by a later
    row -- which is why an undone filing still reads as having happened,
    because it did, and the reversal is its own row beside it. Services
    write through this package rather than opening the file themselves, so
    the receipt shape stays single-owner.

Example:
    >>> from inbox_zero_connector.services.audit import ledger
    >>> ledger.__name__
    'inbox_zero_connector.services.audit.ledger'

See Also:
    - inbox_zero_connector.services.watch: the main writer.
    - inbox_zero_connector.services.reset: the reader that undoes filings.
"""

from __future__ import annotations
