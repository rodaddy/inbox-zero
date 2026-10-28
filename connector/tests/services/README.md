# tests/services

Tests for the connector's service layer, one folder per service.

- `auth/` -- grants, key storage, and uninitialized refusals.
- `triage/` -- classification, taxonomy, corrections, and learned rules.
- `voice/` -- voice profiling and reply drafting.
- `watch/` -- the mailbox sweep loop and its containment boundaries.
- `audit/` -- the append-only ledger and trace records.
