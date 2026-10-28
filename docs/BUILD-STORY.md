# How Inbox Zero was built

Inbox Zero was developed inside a private business repository and published
here as a snapshot. This account comes from that repository's git log for
the kit (246 commits, 2026-08-17 to 2026-10-21), the kit's `CHANGELOG.md`
files and `VERSION`, the specs in `docs/`, the test suite, and the soak
evidence recorded during development.

## The problem

Small teams lose hours a day to email triage, and the obvious AI fix, an
assistant with full mailbox access, is one many businesses will not accept:
an agent that can send can send the wrong thing to the wrong person. The
kit was first requested by a security-minded managed service provider that
wanted to use it on its own mail before offering it to anyone else. The
design goal was an assistant that files and drafts but provably cannot send,
with the guarantee placed where it is hardest to get around: the OAuth
scopes the connector asks for.

## Timeline

| Date | Phase | What changed |
|---|---|---|
| 2026-08-17 | Spec and Stage 1 | A written spec, then a Python MCP connector for Gmail and Microsoft 365 with five tools (`list_inbox`, `get_thread`, `file_message`, `mark_important`, `get_daily_digest`), no send, delete or forward code path, and an append-only ledger the connector writes itself. 84 tests, Ruff and strict mypy clean, plus a scripted no-send check written to fail first. |
| 2026-08-17 | Stage 2 | Voice profiles and never-send drafts, connector hardening, runbook and examples. Six review rounds on the connector that day; 489 tests after round 6. |
| 2026-08-18 to 08-19 | Watcher | A read-only `survey_inbox` tool, the capability map and onboarding spec, the watcher spec, per-folder Microsoft Graph delta polling, a learned-rule store where an owner's correction becomes a rule row instead of a prompt, and a per-client posture (file only, or file and draft). |
| 2026-08-20 to 08-23 | Packaging and beta | Connector restructured to a standard Python layout, deterministic install scripts and an agent installer contract, an OpenAI-backed classifier option, and the first beta builds (0.2.3 to 0.2.7) installed for a pilot user, with fixes from those installs. |
| 2026-08-24 | Tiers | Basic and enhanced tiers, evaluation licence and a scrub of customer zips (0.2.11 to 0.2.13). |
| 2026-09-01 to 09-03 | Voice and recovery | Voice built from an explicit selection of sent mail, a greeting mirror, a "slop gate" that refuses drafts with machine-writing tells, a write-ahead intent journal for mailbox moves, `watch --once --dry-run`, an undo preview, background service registration on three platforms, multi-account rounds, and Outlook delta-sync recovery (0.2.14). |
| 2026-10-21 | 0.2.15 | Documentation pointers corrected. |

## Key design decisions

- **No-send at the scope layer.** The connector never requests a send scope.
  Research during Stage 1 found that Gmail has no scope that allows creating
  drafts without also allowing sending, so on Gmail the drafting tier moves
  the guarantee from the scope layer to the code layer, and the docs say so.
  Microsoft Graph separates the two cleanly.
- **Posture can only narrow.** A per-client posture switch can turn drafting
  off but never on; only the security manifest decides the scope request.
  This rule lives in one function so it cannot be re-derived differently
  elsewhere.
- **Deterministic first.** Filing rules, learned corrections, the greeting
  form and the slop gate are code and data tables. A model is used for
  classification and for writing a draft body, and its output passes
  through deterministic gates that refuse rather than repair.
- **Every move is journalled.** A move is begun, performed, written to the
  ledger and settled; unsettled intents are reconciled at startup, so a
  crash between moving a message and recording it is visible.
- **The model never writes the ledger.** The audit trail is written by the
  connector, never by a tool call an AI can invoke.

## How AI was used to build and check it

The kit was built by AI coding agents directed by one operator, working from
a written spec and, from September, from a planning map of numbered issues
that agents worked as separate lanes, each landing as its own pull request.
Review ran in rounds with different reviewer roles (a subject-matter
reviewer, an antagonist and a security reviewer from a second model family).
The commits record what the rounds caught, for example a round-4 change
that parsed all address headers together, so one malformed header poisoned
the whole thread; round 6 restored per-header parsing.

## Testing and evaluation

- Test counts in the commit history: 84 at Stage 1, 489 after review round
  6, 2,487 on 2026-09-01. The current suite runs 2,718 tests with
  `uv run pytest` in `connector/`.
- Containment tests check what the ledger may contain and that the
  connector does not touch the filesystem or network outside its allowance.
- A live soak ran the watcher for over an hour against a real mailbox to
  prove that an expired Microsoft token refreshes. Because waiting for real
  expiries is slow, the same behaviour is now induced in-process: 25
  expiry-and-refresh cycles and 40 watcher rounds run in four hundredths of
  a second on every test run, including two faults the live soak could not
  produce.
- New tests are written to fail first; commits record the red run before
  the fix.

## What did not work

- The planned Gmail scope for Stage 1 could not deliver "drafts but no
  send"; the spec and copy were corrected the same day.
- Combined header parsing (round 4) was a regression and was reverted to
  per-header parsing in round 6.
- The first beta installs surfaced packaging faults: installer paths that
  assumed one shell and missing per-install isolation, fixed and covered by
  portable installer tests. Later, a keychain entry keyed per provider
  instead of per mailbox was found and fixed.
- A watcher pass that hit a provider fault could stop the process; a fault
  now fails only that pass, and the loop backs off and recovers.

## Current state

Version 0.2.15. Built, tested and soaked against real mailboxes; in pilot
use. The public repository is a snapshot of the kit, not the development
history.
