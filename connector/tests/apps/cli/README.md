# tests/apps/cli

Tests for the `inbox-zero-connector` command line: `setup`, `init-config`,
`paths`, `show-scopes`, `diagnose`, `digest`, `logout`, `reset`, and the rules
surface.

Also covers what the CLI must REFUSE (`test_operator_refusal_surface.py`), the
classifier wiring reaching `watch` (`test_watch_classifier_wiring.py`), and that
importing the CLI cold stays cheap (`test_cold_import.py`).
