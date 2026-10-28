# tests/utils

Tests for the shared helpers in `utils/`.

- `test_atomic_write.py` -- a write either lands whole or not at all.
- `test_logging_console_level.py`, `test_observed.py` -- console log levels and
  the observability wrapper's input/output records.
- `test_no_exc_info_spelling.py` -- enforces one spelling for exception logging
  across the codebase, so the audit checks can find every case.
