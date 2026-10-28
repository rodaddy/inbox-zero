# tests/providers/outlook

Tests for the Microsoft Graph (Outlook) provider, against a fake Graph rather
than live Microsoft.

Covers the auth flow, silent auth, reauth, and the account pin that keeps the
connector on the intended mailbox; folders, delta sync, drafts, tagging, and
sent-sample reads; message-id safety; and retry behavior.
