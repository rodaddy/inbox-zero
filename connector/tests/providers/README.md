# tests/providers

Tests for the mail providers and the provider-neutral contract they share:
container models and label lookup, message ids, date handling, paging limits,
and the retry policy.

Provider-specific behavior lives in `gmail/` and `outlook/`; a test belongs here
only when both providers must satisfy it identically.
