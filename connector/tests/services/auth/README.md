# tests/services/auth

Tests for authentication and credential handling.

- `test_grants.py` -- which scopes are requested and recorded.
- `test_keychain_key.py` -- secrets go to the OS keychain, not to disk.
- `test_uninitialized_refusal_surface.py` -- every entrypoint refuses cleanly
  before auth exists, instead of failing halfway through.
