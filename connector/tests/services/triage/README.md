# tests/services/triage

Tests for triage: deciding where a message belongs.

- `test_classify.py`, `test_classify_learned_pass.py` -- the classify path,
  cold and with learned rules applied.
- `test_taxonomy.py`, `test_label_names.py` -- the folder taxonomy and the
  names written to the mailbox.
- `test_corrections_rule_for.py`, `test_corrections_uncertainty.py`,
  `test_learned.py` -- owner corrections becoming rules, and when the
  classifier declines to guess.
