# tests

The test suite. Runs offline: no network, no credentials, no real mailbox.

    uv run --no-sync pytest

Laid out to mirror `src/`: `apps/`, `classifiers/`, `providers/`, `services/`,
`utils/`. `scripts/` covers the build tooling instead and is never shipped.

- `conftest.py` -- shared fixtures.
- `_gmail_harness.py`, `_outlook_harness.py`, `_mailbox_scope_harness.py`,
  `_watch_harness.py`, `_reset_harness.py` -- fakes standing in for providers
  and long-running loops.
- `test_config_hardening.py`, `test_config_layering.py` -- config precedence and
  the refusals that protect it.
