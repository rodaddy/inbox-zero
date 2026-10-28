# Skills

The three skills the installer puts in front of the host AI. Each folder holds a
`SKILL.md` -- the contract: hard rules, refusals, and what the skill may never
do -- and a `workflows/` folder of step-by-step runs.

- `inbox-triage/` -- sweeping, sorting into the closed folder list, flagging, the
  ledger, and the daily digest. Ships in both tiers.
- `voice-profile/` -- building a style document from sent mail the owner selects,
  and the review gate that must pass before anything drafts. `enhanced` only.
- `draft-replies/` -- writing reply drafts into the owner's Drafts folder, with
  the missing-fact gate. `enhanced` only.

Files the install creates beside these skills -- `taxonomy.md`, `USER-CONFIG.md`,
`branding/`, `ledger/`, and `voice-profile/voice/` -- are never overwritten on
upgrade.
