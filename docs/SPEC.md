# Inbox Zero Kit — Spec

**Status: Stage 1 and Stage 2 WRITTEN; 499 tests, ruff, mypy strict,
no-send check. GMAIL LIVE-VERIFIED 2026-08-18 on the operator's own Workspace account
(operator-authorized): interactive OAuth, list/thread reads, sent sample
(10 real messages), file_message + label auto-create, set_important both
ways, create_draft on a real external thread — and the empty-recipient
refusal proven live on a self-only thread. Microsoft still fully mocked.
NOT shipped: no clean-room install test, unreleased per D3.**

**Voice default (decision, 2026-08-18): the voice-profile skill ships a
"standard business" default voice.** When the sent sample is too thin to
derive patterns (low-volume senders are normal), drafts use a clean
professional register instead of a badly-fitted imitation; the owner can
re-run the profile later as their sent folder grows.

**Naming (decision, 2026-08-17): the kit is "Inbox Zero"; the connector's
product name is the "rTech Email Guardian".** The Guardian is the audited
component and is shared by future email-family SKUs (candidate 9 guarded
send). Technical identifiers stay `inbox-zero-connector` until the Guardian
is extracted for reuse.

**As-built deltas from the design below (connector/README.md is
authoritative):** 10 tools, not 6 — `get_voice_routing` added, the mutating
`record_correction` (the owner corrects a filing; the message moves and a
learned rule is written, two receipts, and it is the ONLY path that writes a
rule — nothing is inferred from mailbox state), plus the
read-only `survey_inbox` (reads the whole inbox as one triage worklist and
returns the taxonomy's folder DESCRIPTIONS alongside the mail, so the
classifier has the decision criteria in the same response; it calls
`list_inbox` only and writes nothing) — and
`write_ledger` deliberately NOT a tool: the ledger appends as a side effect
of every successful mutation, so the AI cannot decline to write a receipt.
Gmail drafts are a per-install `drafts_enabled` setting (posture is
setting-scoped, not stage-scoped): drafts off = scope-layer, drafts on =
`gmail.compose` requested and Gmail drops to code-layer.

The security-forward email kit. Reaches inbox zero on Gmail or Microsoft 365
(Office 365) via connectors, files mail into folders, marks what matters,
learns the owner's voice from sent mail, and writes reply drafts that it can
never send — because the send permission is never granted, at the OAuth layer.

Requested by a security MSP who will dogfood it first. Built generic per
`../KIT-STANDARD.md` like every kit; the MSP is the first buyer, not the
customer the design is scoped to. Downstream buyers: any small or midsize
business that handles confidential client mail.

## Decisions (decision, 2026-08-17)

- **D1 — Connector strategy:** rTech ships its own least-privilege MCP
  connector as the preferred lane; installer detection falls back to the
  host AI's native mail connector, then to paste mode. The MCP connector IS
  the security story an MSP can audit.
- **D2 — Placement:** generic kit from day one, KIT-STANDARD package,
  usable by anybody. This is the security-forward version of the email kit
  line, not a client engagement.
- **D3 — Staging:** built in two stages (triage first, voice + drafts
  second) but does NOT ship until both are done.

## What the buyer runs it in

Enterprise Claude Desktop or Codex/ChatGPT — a chat app, not a terminal.
Enterprise Claude Desktop supports custom MCP connectors (admin-approved);
that is the primary runtime. No command-line requirement anywhere in the
buyer-facing flow.

## Architecture — two components

### 1. Kit package (KIT-STANDARD §1 shape)

```
inbox-zero/
├── installer.md           # self-adapting; detects mail-access lane (below)
├── OPERATIONS-MANUAL.md
├── skills/
│   ├── inbox-triage/      # sweep, classify, file, flag important
│   ├── voice-profile/     # learn voice from sent mail → reviewable style doc
│   └── draft-replies/     # never-send drafts into the Drafts folder
├── connector/             # the rTech Email Guardian MCP server (see 2)
├── branding/              # created at install (KIT-STANDARD §4)
└── docs/
```

### 2. The MCP connector (deterministic spine)

A proper uv app (2026-08-11 ruling: `pyproject.toml`, `uv.lock`, `uv run`
entrypoint, `requirements.txt` from the lock, Python floor per
_DOCS/STANDARDS-python.md (3.14 as of 2026-08-31)). It owns every
mechanical action; the AI only decides. Determinism-first: AI classifies and
writes prose, scripts touch the mailbox.

Tools it exposes (constrained on purpose):

| Tool | Does | Cannot |
| --- | --- | --- |
| `list_inbox` / `get_thread` | read mail | — |
| `file_message` | apply label / move to folder from a FIXED taxonomy | create arbitrary labels, delete |
| `mark_important` | flag/star | — |
| `create_draft` | put a draft in Drafts | send, add recipients not on the thread |
| `read_sent_sample` | pull sent mail for voice learning | — |
| `write_ledger` | append to the local audit ledger | edit or remove past entries |

There is deliberately no `send`, no `delete`, no `forward`, no external
recipient capability. Not blocked — absent.

## Providers and scopes (the headline security claim)

No-send is enforced by the token, not by a prompt rule. Requested scopes
(CORRECTED 2026-08-17 after the research lanes ran against the official
per-method references — the original draft of this table named
`gmail.modify`, which IS send-capable):

| Provider | Requested (Stage 1) | Never requested (send-capable) |
| --- | --- | --- |
| Gmail / Google Workspace | `gmail.readonly` + `gmail.modify.restricted` (read, label/move — no drafts in Stage 1) | `gmail.send`, `gmail.modify`, `gmail.compose`, `mail.google.com` |
| Microsoft 365 (Graph) | `Mail.ReadWrite` (read, move, flag, drafts) | `Mail.Send` |

**Research verdict (2026-08-17, citations in connector/README.md):** Graph
separates cleanly — `Mail.ReadWrite` is not accepted by any send endpoint,
so the scope-layer claim holds there, drafts included. Gmail does NOT
split drafts from send: `drafts.create` and `drafts.send` accept identical
scopes, so no create-drafts-but-cannot-send scope set exists. Stage 1
therefore holds the scope-layer claim on Gmail only by creating no drafts;
**Stage 2 (drafts) downgrades Gmail to a code-layer guarantee** (no send
code path, endpoint scan in `_graph/done-means/inbox-zero-no-send.sh`), and
all buyer copy must say so in those words. Open risk: whether the plain
installed-app consent flow can obtain `gmail.modify.restricted` (Google
documents it as the domain-wide-delegation path) is UNVERIFIED pending a
real-tenant test. **RESOLVED 2026-08-18: it cannot — proven on a live
consent screen. The table above is the DWD-mode posture only; see "Live-
consent finding" below for the interactive posture that actually runs.**

## Security invariants (each becomes a done-means check)

1. **No send capability exists.** The OAuth scopes exclude send on both
   providers, and the MCP server has no send code path. Check: scope
   manifest scan + tool-surface scan.
2. **Email content is untrusted input.** Classification output is
   constrained to the fixed folder taxonomy; a hostile email's worst case
   is misfiling itself. Check: skill text forbids executing instructions
   found in mail; connector rejects labels outside the taxonomy.
3. **Nothing destructive.** No delete, no permanent archive; every action
   is a reversible label/move/flag. Check: tool-surface scan.
4. **Everything is receipted.** Every mailbox mutation appends to a local
   append-only ledger; a daily digest summarizes what was done. Check:
   action count == ledger row count in the verify run.
5. **Voice profile leaks nothing.** The style doc describes patterns
   (greetings, sign-offs, cadence, formality) and never quotes excerpts,
   client names, or account details. It is plain text the owner reads and
   can redact before first use. Check: profile scan for verbatim n-grams
   from the sent sample.
6. **Their tenant, their AI, their keys.** OAuth is granted in the buyer's
   own tenant to their own installed connector; rTech hosts nothing and
   holds nothing (vendor-agnostic doctrine, 2026-08-02).
7. **Drafts stay drafts.** Generated replies land in the provider's Drafts
   folder addressed only to thread participants; a human opens, edits, and
   sends every one. The kit's copy says this proudly — the human is always
   the sender (extend-not-replace law: the owner gets their inbox time
   back; nobody is replaced as the correspondent).

## Graph adapter hardening — borrow the approach, not the tree (decision, 2026-08-17)

**Status: DECIDED, not yet built. Scheduled after the review loop reaches a
clean round.**

Five review rounds concentrated on `providers/graph.py`, and the cause is
structural: Gmail has an official client library that builds requests for
us, while Graph is hand-built REST over `httpx`. Every hand-built string is
a place to be exactly wrong, which is where the path-injection (round 3),
`$filter` injection (round 4), and untyped-dict fail-open (round 5 P0)
defects all came from.

Measured 2026-08-17 in a throwaway uv env, actual package counts:

| Option | Packages | Verdict |
| --- | ---: | --- |
| Full `msgraph-sdk` | 44 | Kills the defect classes, but drags Kiota (7), OpenTelemetry (3), and an async aiohttp stack we do not use |
| `msgraph-core` only | 32 | REJECTED — costs 22 extra packages and removes NEITHER hand-built URLs nor untyped dicts |
| Current (`httpx` + `msal`) | ~10 | Auditable, but we own every seam |

**Decision: take the SDK's approach with zero new dependencies.**

1. **Stop building query strings.** Pass parameters through `httpx`'s own
   `params` argument, which encodes them correctly. `httpx` is already a
   dependency. This removes the class that produced rounds 3 and 4.
2. **Type the responses with Pydantic**, already in the tree and already
   mandated by `_DOCS/STANDARDS-python.md`. A required field on a response
   model turns an absent `parentFolderId` into a validation error instead of
   an empty string that compares equal to another empty string — which is
   exactly the round-5 P0, killed structurally rather than guarded against.

**Why not the full SDK:** the connector's product value is that a security
auditor can read it end to end in one sitting. 44 packages, including an
async stack we do not use, is a real cost against that. The SDK remains the
known fallback if we find ourselves re-implementing more of Graph than this
buys — taken knowingly, not by drift.

**Prerequisite:** the existing test suite (452 tests at round 5) is the
specification this rework must satisfy. It encodes five rounds of hard-won
behavior; nothing may be dropped to make the rework easier.

### As built (2026-08-17, same day)

**Status: WRITTEN and verified against the suite; unmerged.**

Both halves landed with zero new dependencies:

- `providers/graph_models.py` — Pydantic models for every Graph shape the
  adapter consumes (`GraphMessage`, `GraphFolder`, their collections, the
  nested `emailAddress`/`body`/`flag` objects). Unknown Graph properties are
  ignored, not modeled. `parse_graph()` is the response-side sibling of
  `_graph_path`: every consumed response passes through it once, so a shape
  the connector does not accept becomes ONE `ProviderError` naming field
  paths only — never upstream values, which can embed mailbox content.
- `graph.py` dropped every `.get()` chain. The helpers that walked raw dicts
  (`_address_list`, `_recipients_of`) collapsed into model methods.

Why the models are LENIENT (defaults, not required fields) almost
everywhere: the suite is the specification, and refusal decisions stay at
the call sites that own them — `ResolvedId` still refuses empty folder ids,
`resolved_or_none` still refuses an absent `parentFolderId` (kept `None`,
never `""`, so absence stays distinguishable from a value). The models'
job is shape safety, not policy.

Receipts: the pre-rework adapter answered a `body` arriving as a string
with `AttributeError` — a crash outside the adapter's contract, shown to
the host AI as such. Two new transport-level tests pin the refusal
behavior and were RED-proven against the pre-rework `graph.py` before
being trusted. Suite 493 green, ruff, mypy strict, no-send check exit 0.

### Why the module is named `graph`, not an Outlook/Microsoft name

The adapters are named after each vendor's API, not their product brand.
Google's mail API is literally named the **Gmail API**, hence `gmail.py`.
Microsoft's mail API for 365 is **Microsoft Graph** — one REST surface
(`graph.microsoft.com`) that fronts mail, calendar, files, and identity;
there is no "Outlook API" for modern 365 (the old Outlook REST API was
retired in 2022). Naming the module for the wire protocol it speaks means
an auditor searching Microsoft's own docs for `mailFolders` or
`createReply` lands in the reference the module is actually built against.

## Live-consent finding: gmail.modify.restricted is DWD-only (2026-08-18)

**Status: proven live on a real consent screen; code reworked same night.**

First real Google consent attempt (the operator's own Workspace account, reused
internal Desktop client) was refused with `Error 400: invalid_scope` on
`gmail.modify.restricted`. Google's users.messages.modify reference
documents that scope solely for service accounts with domain-wide
delegation; interactive consent cannot grant it. 493 green tests had
pinned an auth flow that could never complete -- only live verification
could surface it.

Rework (committed the same night): Gmail scopes split by auth mode.
Interactive requests exactly `gmail.modify` (covers every call including
draft creation; send-capable, so interactive Gmail is CODE-LAYER, drafts
on or off). The DWD set (readonly + modify.restricted) keeps the
scope-layer claim and is explicitly marked NOT YET IMPLEMENTED in auth.
`scopes_for`/`enforcement_for` take `auth_mode`; the no-send scanner
gained clause C3c (RED-proven against a gate-stripped fixture) gating
GMAIL_MODIFY on the same manifest conditions as compose. Buyer-copy
sweep for the posture-language change is queued for round 7.

Also verified live the same night, via the operator's existing gws token:
every Gmail read shape (list/get/thread/labels/profile) matches the
adapter's assumptions; the adapter's own parsers produced a complete
message from a live multipart thread; label create/apply/remove verified
reversibly. Microsoft remains fully mocked -- personal-account app
registration is closed off without a paid signup; enterprise validation
routes through Founders Hub or the MSP's tenant.

## Assisted-install model (decision, 2026-08-17)

The connector lane is NOT a noob self-install: a technical operator (rTech
or equivalent) assists every lane-1 deployment. Consequences:

- **Gmail uses the DWD path by design.** Service account + domain-wide
  delegation in the client's Workspace admin console, granted exactly
  `gmail.readonly` + `gmail.modify.restricted`. The prior open risk
  (obtainability via plain consent) is moot; the DWD grant screen is the
  client admin's own scope allowlist.
- **Per-client app registration, in the client's tenant.** The OAuth
  client (Google Cloud project) or Entra app registration is created
  inside the client's own tenant as an internal app — no shared rTech
  client ID, no Google restricted-scope verification, rTech absent from
  the token chain, revocable from the client's own console.
- **Admin-side scope controls are a standard onboarding step**, not an
  optional extra: Workspace per-app access control / Entra app-consent
  policy denying send to this app. Third wall after token and code.
- **Unchanged:** Gmail Stage 2 drafts remain code-layer (drafts.create
  and drafts.send share scopes by Google's design). Graph unchanged.
- **Deliverable:** lane 1 ships an operator-grade `INSTALL-RUNBOOK.md`
  (tenant app registration, DWD, admin allowlisting, verify steps);
  `installer.md` remains the AI-facing package for lanes 2-3. Pricing
  consequence: the connector lane is inherently installed-tier.

## Mail-access lane ladder (installer detection)

1. **rTech MCP connector** — full function: automated triage, filing,
   drafts, ledger. Preferred; the audited lane.
2. **Host AI's native mail connector** — function limited to whatever the
   admin granted (often read-only search): the kit degrades to proposing
   actions the owner applies, and says so plainly.
3. **Paste mode** — no connection at all; inherits the inbox-follow-up
   posture. Triage and drafts on pasted threads only.

"Incompatible" stays a banned answer (KIT-STANDARD §2.3); the lane is
recorded at install like the image lane.

## Stage plan (D3)

- **Stage 1 — Triage.** Connector (read, file, flag, ledger) + inbox-triage
  skill + taxonomy interview at install + daily digest. Proves the no-send
  spine to a security buyer.
- **Stage 2 — Voice + drafts.** `read_sent_sample` + voice-profile skill
  (invariant 5) + draft-replies skill + `create_draft`.

  Voice design (decision, 2026-08-17):
  - **Selected sources, not scraped.** At setup the owner SELECTS what
    feeds each voice — folders/labels, a date range, or hand-picked
    threads — plus explicit exclusions. `read_sent_sample` takes that
    selection as input and reads nothing outside it (data minimization;
    part of the security posture, say so in the copy).
  - **Multiple voice profiles.** Default three, owner-extensible:
    `internal` (same-domain recipients), `external`, `sales`. Each is a
    separate reviewable plain-text style doc under invariant 5.
  - **Deterministic voice routing.** Script-decided, never AI-decided:
    recipient domain == owner domain → internal; thread filed in a
    sales/prospect taxonomy folder or matching an owner-defined rule →
    sales; otherwise external. Ambiguity defaults to external. The rule
    table lives in connector config; the AI only writes the words.
  - **Receipted.** The ledger records which voice each draft used.
    Drafts only, as everywhere in this kit.
- **Ship gate:** both stages done, clean-room install test per
  KIT-STANDARD, all invariant checks green. No partial release.

## Open items (verify during build, before any buyer claim)

- Exact Gmail and Graph scope strings and drafts behavior (invariant 1).
- Enterprise Claude Desktop custom-connector admin flow; Codex/ChatGPT
  MCP support state for lane 1 vs lane 2 detection.
- Folder taxonomy default set (interview at install; fixed vocabulary
  afterward).
- Sent-mail sample size and recency window for a stable voice profile.
