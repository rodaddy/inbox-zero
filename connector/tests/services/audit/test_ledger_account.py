"""A receipt names WHICH mailbox it happened in, and old rows still load (#69).

One install now watches every configured account and every one of them writes
into ONE ledger. ``provider`` cannot separate them -- it reads ``google`` for
two Gmail mailboxes alike -- so without the address the file is a merged stream
that no digest, reversal, or morning audit can split back apart.

The second test is the other half of the same contract and the one that is easy
to skip: adding the field must not strand the rows already on a customer's disk.
"""

from __future__ import annotations

import json

from inbox_zero_connector.services.audit import ledger
from inbox_zero_connector.utils.paths import ledger_path


def test_a_receipt_carries_the_account_it_was_written_for() -> None:
    """The address reaches the row through ``Provenance``, like every origin field."""
    entry = ledger.append(
        "google",
        ledger.ACTION_FILE,
        "m1",
        "Action",
        provenance=ledger.Provenance(
            run_id="run-1",
            source_container="Inbox",
            account="second@example.com",
        ),
    )

    assert entry.account == "second@example.com"
    # ON THE WIRE, not merely on the object: the ledger is read back off disk
    # months later by a process that has none of this one's state.
    assert json.loads(entry.to_json())["account"] == "second@example.com"


def test_a_row_written_before_schema_five_loads_with_no_account() -> None:
    """An older row reads back as ``None`` -- unknown, never the default account.

    ``None`` RATHER THAN THE FIRST ACCOUNT'S ADDRESS, and the difference is the
    whole point of the tolerant read. Those rows were written by a watcher that
    served one mailbox and never recorded which, so attributing them to whatever
    account happens to be first today would let a reader splitting the ledger by
    mailbox credit one mailbox with another's filings -- a wrong answer that
    looks exactly like a right one.
    """
    target = ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({
            "timestamp": "2026-08-17T10:00:00+00:00",
            "provider": "google",
            "action": "file_message",
            "message_id": "m1",
            "detail": "Action",
            "status": "applied",
            "run_id": "run-0",
            "source_container": "Inbox",
            "schema": 4,
        })
        + "\n",
        encoding="utf-8",
    )

    rows = ledger.read_all(target)

    assert len(rows) == 1
    assert rows[0].account is None
    # EVERY OTHER FIELD STILL MEANS WHAT IT MEANT. The additive claim in
    # `SCHEMA_VERSION` is only worth making if the old row is otherwise intact.
    assert rows[0].message_id == "m1"
    assert rows[0].run_id == "run-0"
    assert rows[0].schema_version == 4
