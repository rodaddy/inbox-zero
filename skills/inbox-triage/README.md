# inbox-triage

The sorting skill, and the one that ships in both tiers. It sweeps the inbox,
puts every thread into the closed folder list the owner picked at install, flags
the few that need a person today, and reports what it did.

- `SKILL.md` -- the contract: hard rules, classification, flag criteria, the
  ledger format, and the refusals.
- `workflows/` -- the daily sweep, the digest, undo, and taxonomy changes.

It cannot invent a folder, cannot send or delete, and treats instructions found
inside an email as text to report rather than orders to follow.
