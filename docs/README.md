# Inbox Zero -- reference documents

The kit contract files stay at the kit root: `README.md`, `START-HERE.md`,
`installer.md`, `OPERATIONS-MANUAL.md`, `HANDOFF-RULES.md`, `CHANGELOG.md`,
and `VERSION`. Everything below is reference material for rTech operators and
for the lanes that build the kit. None of it is handed to a buyer.

| Document | What it is for |
| --- | --- |
| [SPEC.md](SPEC.md) | The design the kit was built from: scope, naming, the assisted-install model, and the recorded decisions. |
| [CAPABILITIES.md](CAPABILITIES.md) | Capability map and coexistence notes for an MSP owner evaluating the connector against a mailbox that already has rules, folders, and possibly Copilot. Carries the claim-state table. |
| [INSTALL-RUNBOOK.md](INSTALL-RUNBOOK.md) | rTech operator only: per-client app registration in the client's own tenant, scopes, admin consent, tenant-side send denial, MCP registration, and the verification sequence. Never installed to a buyer's machine. |
| [POSTURE-RUNBOOK.md](POSTURE-RUNBOOK.md) | The grant-discovery posture model for the MSP owner: the three postures, and the exact Microsoft Graph and Gmail permission names for each. |
| [WORKSPACE-ADMIN-RUNBOOK.md](WORKSPACE-ADMIN-RUNBOOK.md) | Which admin situation a client is in -- MSP-managed, self-managed owner-admin, or no admin at all -- and what each one changes about the install. |
| [SELF-SERVE-CONSENT.md](SELF-SERVE-CONSENT.md) | Analysis of the third path, where nobody technical is in the room, and what a published multi-tenant app would cost rTech in verification burden. |
| [ONBOARDING-SPEC.md](ONBOARDING-SPEC.md) | The onboarding profile: what the kit asks a new owner, what it derives instead of asking, and why. |
| [WATCHER-SPEC.md](WATCHER-SPEC.md) | The mailbox-wide watcher design: change feeds, watch state, and the drafting pass. |
| [KIT-BOUNDARY-ANALYSIS.md](KIT-BOUNDARY-ANALYSIS.md) | Where the kit ends and the connector begins, and the naming conflicts found across both. |
| [UNASKED-QUESTIONS.md](UNASKED-QUESTIONS.md) | Gaps found by hunting for what was never asked about, with the document and line each one lands against. |
