"""The unattended watch engine - poll, decide, file, receipt, repeat.

One polling round is the unit of work and ``round.poll_once`` is that unit:
process a change page, store the cursor, apply filings, write receipts. The
loop only repeats the round; the round never sleeps or schedules.

Key Components:
    - round: ``poll_once`` - one COMPLETE round for one provider stream.
    - loop: the foreground loop boundary that repeats ``poll_once``.
    - accounts_pass: the multi-account fan-out - one round polls EVERY
      configured account, each with its own provider, scope, and drafts
      answer, and their passes are combined into the single result the
      streaks, the health file, and the backoff already read.
    - state: watch-cursor persistence, so a restart resumes instead of
      re-reading the mailbox from scratch.
    - scope: WHICH folders the watcher may read - one question, one answer,
      one place.
    - receipts: the round's AUDIT-WRITING boundary - the ledger row, the
      trace row, the write-ahead intent. Split from ``round`` because every
      function in it speaks ``services.audit`` and none of them reads a
      cursor, a page, or a taxonomy. It is where a filing's ``run_id`` and
      ``source_container`` are stamped, which is what makes ``audit.undo``
      able to reverse a run.

Pattern/Convention:
    The round is pure orchestration over providers and triage; anything that
    holds state on disk goes through ``state``, and every mailbox mutation
    leaves a ledger receipt. Never widen ``scope`` inside a round - scope is
    configuration, not a runtime decision.

Example:
    >>> from inbox_zero_connector.services.watch import scope
    >>> scope.__name__
    'inbox_zero_connector.services.watch.scope'

See Also:
    - inbox_zero_connector.services.audit.ledger: where receipts land.
    - inbox_zero_connector.providers: the change feeds the round consumes.
"""

from __future__ import annotations
