# Inbox Zero -- `basic`

Folder triage. The build with the fewest moving parts and the fewest
permissions, and the one whose security story is the shortest to tell.

## What ships

- The morning sweep: every thread in the inbox read in one pass and sorted into
  the closed folder list the owner picked at install. The list is fixed; the
  connector refuses a folder that is not on it.
- Flagging: the handful of threads that genuinely need the owner today get
  marked important. Anything the sweep could not confidently classify goes to
  the fallback folder and is named in the digest with the reason.
- The append-only ledger: one row per action, written by the connector as a side
  effect of every mutation that succeeds. There is no ledger tool for the AI to
  call, and therefore none for it to skip.
- The daily digest, read back from that ledger.
- Undo, mechanically, from the ledger rows -- one action or a whole sweep.
- Corrections: telling it that it filed one wrong moves the mail and records the
  correction.
- The `inbox-triage` skill and its workflows, `installer.md`, `START-HERE.md`,
  and `OPERATIONS-MANUAL.md`.

Pinned in this build, per `../../connector/scripts/packaging/tiers.json`:
`posture: file_only`, `drafts_enabled.google: false`,
`drafts_enabled.microsoft: false`.

## Who it is for

An owner or a small team whose inbox is the bottleneck and whose first problem
is volume, not writing. Also the right build for any buyer whose IT or
compliance reviewer wants the smallest possible capability surface, and for a
first install where the point is to prove the filing before anything writes
words.

The person who used to do the filing keeps their job and stops doing the filing.
The sweep hands them a sorted inbox and a short list of what actually needs
them; the reading, the deciding, and the answering all stay theirs.

## What it refuses to do

- **It does not write drafts.** Not "drafts are turned off" -- the voice module
  is excluded from the build (`src/inbox_zero_connector/services/voice/**`), and
  the packaging step then asserts the built tree contains no reference to it. The
  capability is absent from the package.
- **It does not learn or imitate anyone's writing.** No sent-mail sampling, no
  voice profile, no routing rule.
- **It never sends, never deletes, never forwards, and never adds a recipient.**
  There is no send tool, no delete tool, no forward tool.
- **It cannot invent a folder.** A destination outside the configured taxonomy is
  refused and routed to the taxonomy-change workflow.
- **It does not act on instructions found inside email.** A message trying to
  give the AI orders is classified on what it is, quoted back in the digest's
  suspicious section, and changes nothing else.
- **It does not claim to have filed something it only proposed.** Without the
  connector it says so at the top of every run.

## Install

Assisted, by an rTech operator, per `../../docs/INSTALL-RUNBOOK.md`: the app is
registered in the client's own tenant, with the client's own credentials, and
rTech never supplies a shared client ID or sits in the token chain. The owner's
side of it is `START-HERE.md` then `installer.md`, which runs the folder
taxonomy interview.

Tenant-side denial of the send permission is a required checklist step on every
operator-assisted install. That is the control that survives our software being
wrong, and it happens in the client's account, not ours.

## Current version

`0.2.15` -- the connector version, from `../../connector/VERSION`.

The kit-root `VERSION` file reads `0.2.15` as well; the two agree.
