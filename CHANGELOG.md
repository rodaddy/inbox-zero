# Changelog

All notable changes to the Inbox Zero kit are recorded here. This file follows
Keep a Changelog and Semantic Versioning. The kit's version also appears on the
`**Version:**` line in `installer.md` and in `VERSION`.

## [Unreleased]

- 0.2.15 (2026-10-21): Shipped docs no longer cite internal planning paths (#1039).

Documentation caught up to connector behaviour that changed across three review
rounds after 0.2.0 was written. **Docs reconciled to 0.2.13 (2026-09-01):**
`installer.md` and the tier READMEs no longer claim the kit-root `VERSION` is
stale at `0.2.0` — `VERSION` and `connector/VERSION` both read `0.2.13`, and
`installer.md` now reads its version from `VERSION` rather than hard-coding one.
No kit version bump was made in this pass. The connector-side behaviour is recorded in
`connector/CHANGELOG.md` under `Unreleased`.

### Changed — voice selections are a folder/label or a date range

`read_sent_sample` now returns SENT MAIL ONLY and enforces it in code on both
providers, refusing an entire selection that names anything else rather than
returning the remainder. A selection built from thread message ids therefore
normally FAILS, because a thread holds the received side of the conversation as
well as the owner's.

The kit had documented "name a few threads you think sound like you" as one of
three offered selection forms — including as the first scripted line of the
selection interview, which meant the scripted install refused on first use.
Removed everywhere it was offered, and replaced with a conversion script for
owners who name threads unprompted:

- `skills/voice-profile/workflows/selection-interview.md` — the selection
  question now asks for a folder/label or a date range, with an explicit note
  that dates are recorded `YYYY-MM-DD` because any other form is refused
  before a request is made.
- `skills/voice-profile/SKILL.md` — HARD RULE 1, the selection script, the
  no-selection refusal, and the too-little-mail widening suggestion.
- `OPERATIONS-MANUAL.md` — the buyer-facing description of what was asked at
  setup.

### Changed — the build workflow no longer filters received mail client-side

`build-profile.md` step 3 had instructed the AI to discard "anything not
written by the owner (a thread they were on but did not answer)" as an expected
member of the returned sample, and to feed that count into the owner-facing
"41 read, 22 usable" line. That model is gone: received mail is never returned,
so there is nothing of that kind to discard. Step 3 is now a quality pass over
the owner's own messages, and both counts are stated as counts of sent mail.

Added step 2a: a refusal routing table. Each connector refusal is named with
what it means and what to do, and the workflow states that a refused call is
never retried with a wider selection.

### Added — two refusals the buyer-facing lists were missing

`OPERATIONS-MANUAL.md` presented "When it refuses" as the complete list of
voice-profile refusals (no selection, too little mail, needs a quote), and
`skills/voice-profile/SKILL.md` carried the same three. Both now also document:

- **a selection naming mail the owner did not send**, refused whole rather than
  trimmed, with the reason a short list is worse than a refusal; and
- **a date bound that is not a calendar date**, refused before any request.

### Added — the manual states what happens when proof is missing

The sent-only guarantee now says explicitly that a message the kit cannot PROVE
was sent — because the provider returned no folder information for it — is
turned down rather than let through. This closes a gap where the Microsoft lane
could pass a message it could not prove; the connector-side fix and its
reasoning are in `connector/CHANGELOG.md` under `Unreleased`.

## [0.2.0] - 2026-08-17 — UNRELEASED

**Stage 2 kit package (voice + drafts). WRITTEN, not installed, not tested, not
for sale.** Both stages are now present as written files. The ship gate is
unchanged and is NOT met: it requires both stages plus a clean-room install
test, and no install of any kind has been performed.

This entry covers the kit-package layer only — skills, docs, examples, and the
installer. Connector Stage 2 tool work (`read_sent_sample`, `create_draft`,
`resolve_voice`) is recorded in `connector/CHANGELOG.md`.

### Added

- `skills/voice-profile/SKILL.md` + four workflows
  (`selection-interview.md`, `build-profile.md`, `review-and-approve.md`,
  `update-voice.md`). The voice-learning contract:
  - **Selected sources, never scraped.** The owner names folders/labels, a date
    range, or specific threads per voice, plus explicit exclusions, recorded in
    `voice/selections.md`. `read_sent_sample` is called from that record and
    nothing else, and the workflow forbids widening a selection to improve a
    result. *(Superseded in Unreleased: the thread form is withdrawn — the
    connector returns sent mail only and refuses a selection naming received
    mail.)*
  - **Patterns only, never quotes.** No verbatim fragments, no correspondent or
    company names, no subject lines, no figures/dates/addresses from sampled
    mail. The owner's first name is permitted in exactly one place (the recorded
    sign-off form). Examples, where a shape needs one, are invented on made-up
    content and labeled invented.
  - **A review gate the drafter enforces.** New profiles are
    `reviewed by owner: no`, and `draft-replies` refuses to use one in that
    state. Approval attaches to a specific file, so a rebuild resets it.
  - **A mandatory `What I could not tell` section** — what the sample did not
    contain, so a thin read cannot masquerade as full knowledge.
  - **Three refusals, each with a stated trigger:** no selection configured
    (never defaults to "the whole Sent folder"); fewer than **15 usable
    messages** the owner actually wrote, after discarding forwards, one-word
    replies, auto-replies and calendar accepts (the floor is stated as a number
    alongside the count actually obtained); and a pattern that would need a
    quote to be useful, which routes to an owner-pasted boilerplate block marked
    as owner-supplied rather than lifting the text. *(Superseded in Unreleased:
    five refusals — a non-sent selection and an invalid date bound were added.)*
- `skills/draft-replies/SKILL.md` + three workflows (`draft-one-reply.md`,
  `draft-batch.md`, `missing-fact-gate.md`). The drafting contract:
  - **Voice comes from the connector's `resolve_voice`, never from the AI.**
    Own domain → internal; sales folder or owner rule → sales; everything else
    and everything ambiguous → external. On the degraded lanes the AI applies
    the same rule mechanically and announces which and why, rather than judging
    tone.
  - **Untrusted-input hardening aimed at the specific Stage 2 risk:**
    instructions inside the very message being replied to are content to answer,
    never commands — and a request aimed at the AI is not answered as though it
    were the sender's request. A thread can never grant permission; content from
    an incoming message is never restated as the owner's own fact.
  - **The missing-fact gate**, with the hedge-answer check: a number must be a
    single figure, a date a specific day, a decision yes or no. A range is
    refused a second time and the two real options are offered. Never average,
    never round, never "approximately".
  - **Recipients from the thread only** — no CC, no BCC, no additions, including
    when the thread's text asks for it.
  - **Batch caps** (ten with a connector, five on propose-only lanes) with three
    reported lists: drafted, needs-an-answer-from-you, and nothing-to-reply-to.
    Drafting is never described as answering, and no thread is moved on the
    assumption that a draft equals a reply.
  - **Refusals:** no profile for the resolved voice (and never substituting
    another voice's); an unreviewed profile; a missing fact; a hedge answer; no
    reply-able participant; and a thread that is nothing but injected
    instructions.
- `docs/INSTALL-RUNBOOK.md` — operator-grade assisted-install runbook for lane 1,
  per the SPEC's assisted-install model. Per-client app registration inside the
  client's OWN tenant (Google Cloud internal project / Entra app registration,
  never a shared rTech client ID); Gmail service account with domain-wide
  delegation granted exactly `gmail.readonly` + `gmail.modify.restricted`, plus
  `gmail.compose` ONLY on a drafts-on install with the code-layer consequence
  stated at the moment the operator types it; Entra `Mail.ReadWrite` delegated
  with admin consent and nothing else; tenant-side send denial as a required
  checklist step; Claude Desktop / Codex MCP registration with the two
  policy blockers named; and a verification sequence ending in the done-means
  check and a supervised first sweep on the client's own mail. Not installed to
  the buyer's machine.
- `examples/` — the KIT-STANDARD worked-example layer, previously flagged
  missing. Five files plus a README: a full morning-sweep transcript (including
  two refusals and a message trying to instruct the AI), a filled `taxonomy.md`
  with the refusal that kept the fallback folder, a five-section daily digest, a
  redacted voice profile with an explicit list of what it does NOT contain, and
  one draft reply showing the missing-fact gate stopping twice with its JSONL
  ledger row. **All content synthetic**, labeled as such at the top of every
  file, `example.com` domains throughout, with the standing rule restated that
  examples can never serve as install verification.

### Changed

- `installer.md` → 0.2.0. Installs all three skills as siblings (so
  `draft-replies` resolves `../voice-profile/voice/<voice>.md`); three zips on
  the 3B path rather than one; 3C source list and preserve-list extended to the
  drafting contract, the gate, and the voice rules; upgrade protection extended
  to the entire `voice-profile/voice/` folder, with a named 0.1.x upgrade path.
  Two new sections:
  - **Section 3H, the drafts decision.** On Microsoft it is a plain yes/no with
    no trade-off. On Gmail the trade-off is spelled out to the owner in plain
    words before they answer, and the answer is written to `USER-CONFIG.md` as
    `drafts enabled:` and `gmail no-send enforcement:` (`scope-layer` off,
    `code-layer` on). Drafts-on is explicitly NOT the default, and the
    tenant-level send block is recommended out loud.
  - **Section 3I, the voice-selection interview.** The three default voices,
    the per-voice selection and exclusion questions, the sample size said back
    BEFORE anything is read, and the handoff to the skill (which owns the floor
    check, the no-quoting rule, and the review gate). Custom voices require a
    mechanical routing rule or are folded into `external`.
  - Verify step now includes a draft on a real thread of the owner's, naming the
    voice used and why, and stating that the gate firing is the verification
    succeeding.
- `OPERATIONS-MANUAL.md` → 0.2.0. Three new sections — voice profiles, reply
  drafts, and "What changes on Gmail when you turn drafts on" — plus quick-
  reference rows. The Gmail security section is restated from Stage-1-vs-Stage-2
  to drafts-off-vs-drafts-on, since it is now a setting rather than a release.
- `README.md` → 0.2.0. Stage 2 present rather than absent; a **per-provider
  guarantee table** (Microsoft permission-layer in both columns; Gmail
  permission-layer with drafts off, code-layer with drafts on) with the explicit
  instruction never to say "structurally cannot send" as a blanket claim; box
  contents; T2/T3 tier rows stating what Stage 2 specifically loses at each
  tier; and twelve new acceptance boxes covering the voice floor, no-quoting,
  the review gate, voice routing, the missing-fact and range refusals, the
  recipient boundary, drafts-off behavior, examples honesty, and the runbook.
- `START-HERE.md` — one added line pointing at `examples/`.
- `VERSION` → `0.2.0`.

### Security claims — what changed and why

The Gmail posture is now stated **per drafts setting**, not per stage, because
it is a decision the buyer makes at install rather than a property of the
release:

- **Microsoft Graph — permission-layer in both cases.** `Mail.ReadWrite` covers
  drafts and is not accepted by either send path; enabling drafts changes
  nothing. Unchanged from 0.1.0.
- **Gmail — permission-layer with drafts OFF, code-layer with drafts ON.**
  `drafts.create` and `drafts.send` accept an identical scope set, so drafts
  require `gmail.compose`, which is send-capable. Every buyer-facing file now
  carries that distinction in those words, and the enforcement level is written
  into `USER-CONFIG.md` on the install itself so nobody has to remember which
  posture a given customer is on.
- **The third wall is named everywhere it belongs:** the buyer's own
  administrator blocking the send scope for the app at the tenant level, which
  is the only control that survives rTech's software being wrong. It is a
  required checklist step in `docs/INSTALL-RUNBOOK.md` §4, recorded with who applied
  it and when, and recorded as `no` when the client declines.

**Softened / not claimed:** the runbook cites CISA's SCuBA secure-configuration
baselines as documentation that tenant-level restriction of high-risk OAuth
scopes is standard guidance, and marks that citation as **not re-verified
against CISA's current published baseline in this session** — the operator is
told to confirm the control ID before quoting a number to a client's security
team. No claim is made about which control number applies.

### Not present / not done

- No install, of any kind, on any harness. Every harness row in the README
  remains untested and every acceptance box remains open.
- No live scope grant on any real mailbox, on either provider, in either
  posture. The scope tables are documentation review from 2026-08-17, not a
  tenant test.
- No step of `docs/INSTALL-RUNBOOK.md` has been executed against a real tenant.
- Connector Stage 2 tools are outside this entry's scope; the kit package is
  written against the tool surface named in `docs/SPEC.md`. Until those tools exist
  and are green, every lane-1 behavior described in the Stage 2 skills is
  WRITTEN, not RUNNING.
- `branding/` is still created at install, as designed, and does not ship.

### Verification

None for this entry. Written files only. Treat every capability statement in the
Stage 2 material as WRITTEN, not RUNNING.

## [0.1.0] - 2026-08-17 — UNRELEASED

**Stage 1 of 2 (triage). WRITTEN, not installed, not tested, not for sale.**

Per Rico's ruling 2026-08-17 (docs/SPEC.md decision D3), this kit ships only when
Stage 2 — voice profile and reply drafts — is also complete and both stages have
passed a clean-room install test. There is no partial release. A 0.x version
number is the record of that: this package exists to be dogfooded and beaten on,
not sold.

### Added

- `installer.md` — the full KIT-STANDARD §2 skeleton (runtime detection 3A–3D
  with "incompatible" banned, collision check with the version-aware upgrade
  path and a protected-data list, recursive copy, image-capability detection,
  branding interview, own-data verification, if-anything-fails), plus two
  kit-specific sections:
  - **Section 3F, mail access lane.** Three-lane detection ladder — the rTech
    connector, the host AI's own mail connection in propose-only mode, and paste
    mode — recorded to `branding/mail-lane.md` with `can send mail: no` written
    into the file on every lane. Carries the researched runtime-support facts
    per host, including the two items recorded as UNVERIFIED.
  - **Section 3G, folder taxonomy interview.** Offers the default seven (Action
    Needed, Waiting On, Finance, Clients, Vendors, Newsletters,
    Archive-Reference), the owner edits, twelve-folder ceiling, fallback folder
    mandatory, written to `taxonomy.md` and — on the connector lane — to the
    connector's own configuration location so "closed set" is mechanical rather
    than a promise.
- `OPERATIONS-MANUAL.md` — the driving manual: the morning sweep, the daily
  digest, undo, how to read the ledger, and the per-provider explanation of why
  "it cannot send" is believable, stated at the honesty level each provider
  actually supports.
- `skills/inbox-triage/SKILL.md` — the triage contract. Six hard rules
  (untrusted email content, labels only from the taxonomy, unsure goes to the
  fallback folder, never ask the connector for anything outside its tool
  surface, never send or delete or reply, never claim an action not taken),
  the classification ladder, the flagging criteria with an eight-flag ceiling,
  the append-only ledger format, five named refusal conditions, and explicit
  degraded-mode behavior for lanes 2 and 3.
- `skills/inbox-triage/workflows/` — `morning-sweep.md`, `daily-digest.md`,
  `undo-filing.md`, `taxonomy-change.md`.
- `README.md`, `START-HERE.md`, `VERSION`, this file.

### Security claims — written from a documentation review, 2026-08-17

The provider claims in `README.md` and `OPERATIONS-MANUAL.md` were written
against Google's and Microsoft's own published references, not against
assumption, and they say different things on purpose:

- **Microsoft Graph:** `Mail.ReadWrite` covers list, move, flag, and draft
  creation, and Microsoft's own text for that permission states it "does not
  include permission to send mail." Sending requires `Mail.Send`, which is never
  requested and which Graph's send endpoints demand. The docs also record the
  two limits: `Mail.ReadWrite` is whole-mailbox (no folder-scoped alternative
  exists), and delegated permissions can be requested again at sign-in, so the
  claim is written as "the token this kit uses cannot send" rather than "sending
  is impossible."
- **Gmail:** **the SPEC's original headline claim is refuted for the kit as
  specified and the docs were softened accordingly.** Google lists identical
  permissions for creating a draft and sending that draft, so no
  drafts-without-send permission exists. Stage 1 creates no drafts and therefore
  can hold a read-plus-restricted-modify set that Gmail's send endpoint does not
  accept — that claim is stated as permission-layer. Stage 2 adds drafts and the
  docs already state, in advance, that the Gmail guarantee becomes **code-layer**
  (no send code path, send endpoints never called) and must be described in
  those words. The docs also name the only control that survives our software
  being wrong: the buyer's own Workspace administrator restricting which
  permissions the app may request.

### Runtime support — written, with the unverified items named

Local connector support is recorded as supported on Claude Desktop
(Team/Enterprise, Owner-uploaded desktop extension, 0.13.91+) and on Codex; not
supported on claude.ai web or ChatGPT web, both of which accept only remote
connectors. The two blockers that beat an Owner's decision — an organization
extension allowlist and device-management policy on the machine — are stated in
both the installer and the README.

**Recorded as UNVERIFIED and to stay that way until someone runs it:** whether
device-management policy can require the connector to be signed, and whether a
managed list of allowed connectors can be pushed to Claude Desktop machines.
Both surfaced only in search summaries and were not found in the vendor's own
configuration documentation on 2026-08-17.

### Not present

- Stage 2: `skills/voice-profile/`, `skills/draft-replies/`, and the
  connector's `create_draft` and `read_sent_sample` tools. *(Both skills landed
  in 0.2.0; the connector tools are tracked in `connector/CHANGELOG.md`.)*
- `examples/` (KIT-STANDARD worked-example layer) — required before ship, not
  written yet. *(Landed in 0.2.0.)*
- Any acceptance-test receipt. Every box in the README's acceptance list is
  open. Nothing in this kit has been installed or run.

### Verification

None. This entry records written files only. No install has been performed, no
sweep has been run, no scope has been granted on any real mailbox, and no
harness row in the README has been tested. Treat every capability statement in
this package as WRITTEN, not RUNNING.
