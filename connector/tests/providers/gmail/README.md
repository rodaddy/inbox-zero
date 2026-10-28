# tests/providers/gmail

Tests for the Gmail provider, against a fake API rather than live Google.

Covers basics and label/watch handling, the container-listing and label-id
contracts shared with Outlook, draft creation, the query boundary that keeps
searches scoped, and the vanished-message case where a message disappears
mid-sweep.
