# Inbox Zero -- tiers

Two tiers, one codebase. Both `basic` and `enhanced` are build outputs of the
connector under `../connector/`, and what goes into each zip is decided by
`../connector/scripts/packaging/tiers.json` rather than by hand.

| Tier | What it is | Notes |
| --- | --- | --- |
| [`basic`](basic/README.md) | Folder triage only. Posture pinned to `file_only`, drafts pinned off, voice module not built into the package at all. | The security-simplest build: the capability is absent, not disabled. |
| [`enhanced`](enhanced/README.md) | Everything in `basic`, plus the voice profile and reply drafts. | Declared as `enhanced` in the packaging config (renamed from `full` on 2026-08-24). |

Neither tier can send, delete, or forward mail. The owner opens, edits, and
sends every message that goes out with their name on it, in both tiers.

**Version.** The connector version is the one that means anything here:
`../connector/VERSION` reads `0.2.15`, and the kit-root `VERSION` file matches at
`0.2.15`. Quote `0.2.15` when someone asks what the software is at.
