# tests/services/audit

Tests for the audit ledger: the append-only record of what the connector did.

- `test_ledger.py`, `test_ledger_correction.py` -- entries and corrections.
- `test_ledger_deterministic.py` -- the same run writes the same ledger, so a
  diff is evidence rather than noise.
- `test_trace.py` -- per-operation trace records.
