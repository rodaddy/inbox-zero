# Inbox Zero Connector — Capability Map and Coexistence

**Audience:** the MSP owner or senior engineer evaluating whether this thing can be
deployed into a mailbox that already has fifteen years of rules, a client-numbered
folder tree, and possibly a Copilot licence, without breaking any of it.

**What this document is.** An inventory of what the connector can do, how each
capability would be implemented, and how each one sits alongside whatever the client
already runs. It is not a comparison sheet and it takes no position that a client
should stop using anything they have. Everything here is additive by design: the
product's job is to add a signal the mailbox does not currently carry, never to take
over a job something else is already doing.

**What this document is not.** It is not a claim that this software is finished. Read
the status table before anything else.

---

## 0. Status — read this first

Claim states, per the repo's claim-state law:

- **RUNNING** — live and observed working this session.
- **MERGED** — in `main`; nobody has proven it runs.
- **WRITTEN** — source or doc exists on disk. Not merged.
- **PROPOSED** — somebody said it. Not written.

**Nothing in this connector is MERGED.** All connector source sits on the branch
`feat/inbox-zero-kit` and has never reached `main`. Two modules central to the
capabilities below — `src/inbox_zero_connector/classify.py` and
`src/inbox_zero_connector/watchstate.py` — are **untracked in git entirely**
(`git log --all` returns nothing for them), so they are WRITTEN in the weakest sense:
they exist as files and nothing more.

Verified this session:

| Observation | How checked |
| --- | --- |
| 627 tests pass, 1 fails | `uv run pytest -q` in `connector/`. The failure is `test_the_package_is_within_the_code_line_ceilings` — `config.py` is 510 code lines against a 500 ceiling. Pre-existing, owned by another lane, untouched here. |
| Connector source is on a feature branch, not `main` | `git branch --show-current` → `feat/inbox-zero-kit` |
| `classify.py`, `watchstate.py` never committed | `git log --oneline --all -- <path>` returns empty |
| One live artifact exists | An Entra consent screen (screenshot kept privately, not published) showing Graph permissions granted |

**The honest summary:** the MCP tool surface, the Outlook and Gmail providers, the
taxonomy, the ledger, and the scope manifest are WRITTEN and covered by a passing
test suite. The category-writing capability, the mailbox-wide watcher, and the
onboarding profile are **PROPOSED** — they are designs derived from the research
briefs and nothing more. A reader who takes this document as a feature list of
working software has been misled, and that is the failure this section exists to
prevent.

---

## 1. Sources

Three research briefs, all marked WRITTEN, research-only, produced 2026-08-18. Cited
throughout by short name:

- **`audit-brief`** — `outlook-native-tools-brief.md`. What Microsoft already ships:
  the full `New-InboxRule` surface, Focused Inbox, Sweep, Clean Up, Quick Steps,
  Categories, Copilot in Outlook, and a deliberately unflattering assessment of where
  this product is redundant.
- **`rules-brief`** — `rules-interop-brief.md`. Graph capability findings on folder
  enumeration, mailbox-wide message listing, per-folder delta, and categories as a
  non-destructive classification instrument.
- **`watch-brief`** — `graph-watch-brief.md`. Trigger mechanism: subscriptions,
  webhook reachability, delta as the pull-model peer, throttling, multi-tenant
  consent.

Each brief carries its own confidence markers (DOCUMENTED / INFERRED / UNVERIFIED).
Those markers are preserved here. Where a claim is UNVERIFIED it says so; where the
briefs disagree, both readings are given.

---

## 2. Capability inventory

Nine capabilities. For each: what it does, claim state, permission cost, how it would
be implemented, and — the column that matters most to a buyer — what it cannot do.

### 2.1 Read mail (list, thread, sent sample)

**State: WRITTEN.** Implemented in `providers/outlook.py` (`list_inbox`, `get_thread`,
`read_sent_sample`) and the parallel Gmail provider. Exposed as MCP tools
`list_inbox`, `survey_inbox`, `get_thread`, `read_sent_sample`.

**Permission:** `Mail.ReadWrite` on Graph (already held); `gmail.modify` interactive
or `{gmail.readonly, gmail.modify.restricted}` under DWD on Google. Reading alone
would only need `Mail.Read` / `gmail.readonly`; the write scope is held for §2.3
and §2.4.

**Implementation:** `GET /me/messages` with an explicit `$select` projection.
Per the rules-brief this call is genuinely mailbox-wide — Microsoft's own description
is "the messages in the signed-in user's mailbox (including the Deleted Items and
Clutter folders)" (DOCUMENTED). Paging follows the opaque `@odata.nextLink`; the brief
warns explicitly against extracting or manipulating `$skip`.

**What it cannot do:**
- Cannot read the Exchange Online **Archive mailbox**. It is a separate mailbox and
  the rules-brief marks this INFERRED-excluded, UNVERIFIED. For a firm with aggressive
  archiving policy this is a real blind spot, not a footnote.
- **Junk Email, Sent Items, Drafts, and Outbox coverage is INFERRED, not documented.**
  Deleted Items and Clutter are named in the docs; the rest is reasoning. Verify on a
  live tenant. Independently, Sent Items and Drafts should be excluded in application
  code by `parentFolderId` even if reachable — classifying the user's own half-written
  drafts is noise.
- Cannot read attachment **contents**. The provider projects headers and body, not
  parsed PDFs or DOCX. For a business whose substance is routinely in the attachment,
  this is a stated gap. The audit-brief notes native rules cannot do this either
  (`HasAttachment` is boolean) — so this is unclaimed ground for everyone, not a
  weakness relative to what the client has.
- `mailSearchFolder` entries are **virtual views, not storage** (DOCUMENTED). A message
  in a search folder physically lives elsewhere. They must be filtered out by
  `@odata.type` or the same message is processed twice.

### 2.2 Classify a message into the taxonomy

**State: WRITTEN, untracked.** `classify.py` exists on disk and has never been
committed. Nine-folder vocabulary in `taxonomy.py` (committed).

**Permission:** none. The module performs no I/O — no provider, no network call, no
ledger write. It is a pure function from a message value object to a `Classification`.

**Implementation:** deterministic rules first, model second, per the repo's
determinism-first law. `RULES` is an ordered data table of (predicate, folder, reason)
rows; the first match wins and short-circuits. A model is asked only about the
remainder. The module's own docstring states the security rationale plainly: a rule
cannot be talked into anything by attacker-controlled subject text, so every message a
rule settles is one a hostile email never gets to influence.

The taxonomy is closed and fixed at install time. There is no add-folder tool. A
hostile email's worst case is misfiling *itself* into one of nine folders the owner
already knows about — reversible, and receipted either way.

**What it cannot do:**
- Cannot widen its own vocabulary at runtime, by design.
- Cannot resolve **solicited vs. unsolicited**. The audit-brief is explicit: this
  depends on whether *this user* ever opted in — historical consent, invisible in the
  message. A rule cannot know it and an AI reading only the message cannot either.
  Only interaction history can. Be careful claiming this one.
- Cannot reliably separate a **paid receipt from an unpaid invoice** in the general
  case (audit-brief: NO for the general case, PARTIAL in practice). Fails on vendors
  whose paid and unpaid mail share a subject, on attachment-only invoices, and on
  threads carrying both. The audit-brief notes the cost is asymmetric — misfiling an
  unpaid invoice as a receipt is how a firm misses a payable.
- Produces a probabilistic answer. The `Review` folder exists precisely because the
  classifier can be unsure, and the audit-brief calls that the honest safety valve
  that makes any auto-action defensible in a business with real deadlines.

**Where this capability is genuinely unserved by anything native:** the
Business-Action / Business-Waiting split. The audit-brief calls it "the strongest no in
the brief" — it is not a property of the message but of conversation state plus who
spoke last plus whether the last speech act was a request or a commitment. "I'll get
you the redline by Thursday" and "Can you send me the redline by Thursday?" are
byte-similar, same sender, same thread, identical headers. No `New-InboxRule` condition
separates them; Copilot Prioritize does not attempt it (it emits high/normal/low, not
who owes whom); Focused Inbox is binary importance. This is the one claim that survives
the audit intact.

### 2.3 Apply categories (label without moving)

**State: PROPOSED.** This is the rules-brief's central recommendation and **no code
implements it.** Searching `src/` for `categories` finds only *reads* — the Outlook
provider selects `categories` in its `$select` projection and uses them to filter a
sent-mail sample. There is no category **write** anywhere in the package.

**Permission for the write:** `Mail.ReadWrite` — already held, no new consent
(DOCUMENTED). `categories` is a `String collection` on `message` and is explicitly
listed as updatable on `PATCH /me/messages/{id}`, notably without the `isDraft = true`
restriction that constrains `subject` and `body`, so it is writable on delivered mail.

**Permission for the master-category list:** `MailboxSettings.ReadWrite` — **not
currently held.** See §5.

**Implementation:**

```http
PATCH /me/messages/{id}
{ "categories": ["rTech: Business-Action"] }
```

**Two hazards, both load-bearing:**

1. **A `categories` PATCH may replace the collection rather than append to it.** The
   rules-brief marks this **INFERRED from OData collection-property semantics and
   UNVERIFIED against a live tenant**, and flags it as the thing that must be tested
   deliberately because the blast radius is client data. If it replaces, sending a bare
   array silently destroys every category the client assigned themselves. The required
   discipline is read-union-write: read existing `categories`, union our label in,
   write the union back. *(A live test of this was reported in progress at the time of
   writing. Until its result is recorded here, treat replacement as the assumption and
   implement read-union-write regardless — the safe implementation is correct under
   both outcomes.)*
2. Use the ETag. `message` responses carry `@odata.etag`; an `If-Match` on the PATCH
   turns a lost-update race against the user's own open Outlook client into a `412`
   that can be retried (INFERRED).

**What it cannot do:**
- Cannot move the message, and this is the point. `parentFolderId` is not in the
  `message-update` writable set at all (DOCUMENTED); relocation is a separate `move`
  action. A categories PATCH has no folder side effect (INFERRED, though about as safe
  as an inference gets).
- Cannot show a **colored** label without a master-category entry. Without
  `MailboxSettings.ReadWrite` the assignment is believed to succeed and render grey
  (INFERRED on the success, UNVERIFIED on the color fallback — no Microsoft Learn page
  states it outright).
- **Visibility on Outlook mobile is UNVERIFIED.** Desktop and OWA are not in question;
  mobile — specifically whether a category shows in the message *list* versus only on
  the open item — has no Microsoft page enumerating it. The whole value proposition is
  "the user can see our classification," so this is not a detail to wave through. It is
  a five-minute check against a real device.
- No documented cap on categories per message, master-list size, or name length
  (all UNVERIFIED). Irrelevant at nine categories; would matter if the taxonomy grew or
  merged with a client's own.

### 2.4 File into folders (move)

**State: WRITTEN.** `OutlookProvider.file_message` is implemented and exposed as the
MCP tool `file_message`.

**Permission:** `Mail.ReadWrite` (DOCUMENTED). Graph has no folder-scoped OAuth
permission — even the folder-scoped `POST /me/mailFolders/{id}/messages` requires full
`Mail.ReadWrite`. The scope manifest states this plainly: the defensible claim is
"cannot send," never "narrowly scoped."

**Implementation:** `POST /me/messages/{id}/move` with a resolved `destinationId`.
Graph's DELETE verb is never issued. One mechanical trap already handled in the code:
`/move` is copy-then-delete, so the message in the destination is a **new entity with a
new id** and the original id is dead the moment the move lands. The ledger records the
new id.

**What it cannot do — and why this capability should be treated as opt-in, not default:**

The audit-brief is direct about the risk: *"Auto-filing out of the Inbox creates a
missed-deadline liability in any business with real deadlines. A misfiled
Business-Action is a business risk, not a UX annoyance. Categories or a Review-first posture
are the safer defaults; silent moves are the dangerous one."*

Independently, the rules-brief reaches the same conclusion from the API side: a move
out of a folder appears in delta as `@removed` with `"reason": "deleted"` — **identical
to an actual delete** — because "delta tracking operates at the collection level"
(DOCUMENTED). Nothing in the payload records who moved it, and no rule-attribution
field exists. So a connector that moves mail can never distinguish its own move, the
client's rule's move, and the user's manual filing.

The rules-brief's conclusion: *"do not try to attribute moves. Make the watcher
indifferent to who moved the message"* — which is exactly what categories (§2.3) buy.
The failure mode "our watcher re-files what their rule already handled" is not
mitigated in a categories-only design; it is structurally absent.

**Note the tension between §2.3 and §2.4 honestly.** Filing is the capability that is
built; categorising is the capability the research recommends. That is the current
state of the product and this document does not paper over it.

### 2.5 Draft replies

**State: WRITTEN.** `create_draft` implemented on both providers; the reply-draft
action builds recipients server-side, so no code path here can add a recipient.

**Permission:** `Mail.ReadWrite` on Graph — and this is where the two providers
genuinely differ, which the scope manifest documents at length and warns must not be
flattened in buyer copy:

- **Microsoft:** `Mail.ReadWrite` covers draft creation including `createReply`, and is
  accepted by **neither** send path (`POST /me/sendMail`, `POST /me/messages/{id}/send`
  both require `Mail.Send`). The no-send guarantee stays **scope-layer** — Microsoft's
  servers refuse the send.
- **Google:** `users.drafts.create` and `users.drafts.send` accept an *identical* scope
  set. There is no compose-without-send scope in the Gmail API. Enabling Gmail drafts
  downgrades the guarantee to **code-layer** — this connector has no send code path,
  proven by a lexical scanner every build, but the token itself could send.

The connector computes this posture rather than hardcoding it
(`scopes.enforcement_for`), so buyer copy can be generated from the deciding function
and cannot go stale when someone flips a config.

**Two switches gate this, and one of them is per CLIENT.** `drafts_enabled` above
is per PROVIDER because on Gmail it decides an OAuth scope. `posture` is per
client and answers a different question — what this deployment should be allowed
to do at all. `file_only` (the DEFAULT) files mail and writes no drafts;
`file_read_draft` permits drafting where `drafts_enabled` also allows it.

**Posture can only NARROW, never widen.** `file_read_draft` does not enable
drafting on its own, because that would give the scope decision above a second
and weaker source. `file_read_draft` with `drafts_enabled` false produces zero
drafts and logs a WARNING at every start naming both fields and the command that
resolves it. `posture` never reaches `scopes.py`, which is asserted structurally
by a test on that module's own source.

**`file_read_draft` does NOT mark mail read.** Filing does not change read state
under either posture and no code in this kit touches the unread flag. The "read"
in the name is the reading the connector does to classify, not a mutation.

**What it cannot do:** send. `Mail.Send` is deliberately not requested. One caveat the
manifest insists on keeping in buyer copy: delegated permissions support dynamic
consent, so omitting `Mail.Send` from the app registration is not by itself an
unbypassable barrier. The correct sentence is "the token this kit uses cannot send,"
never "sending is impossible." Tenant-side hardening (Entra consent policy, Exchange
`ApplicationAccessPolicy`) is what makes it structural, and that is an onboarding step,
not a product default.

### 2.6 Digest and reporting

**State: WRITTEN (thin).** A `digest` CLI command exists. Every mutation writes a
ledger receipt (`ledger.py`); receipts are a side effect of a mutation succeeding, not
a separate tool the AI can be talked into calling.

**Permission:** none beyond what the mutation already used. The ledger is local.

**Implementation:** local append-only record. Nothing token-like is written to it;
logging redaction is separately tested (`test_logging_redaction.py`).

**What it cannot do:** it is not a management console. There is no multi-tenant
aggregation, no cross-mailbox rollup, and no MSP-facing dashboard. The audit-brief
argues the MSP multi-tenant control plane "is arguably the actual product, more than
the classifier is" — it is PROPOSED and unbuilt.

### 2.7 Watch continuously

**State: PROPOSED, with one WRITTEN piece.** `watchstate.py` exists (untracked) and
implements delta-cursor persistence: opaque delta links, resync generations, and a
seen-set for replay suppression. It performs **no network I/O** and holds no Graph
client — deliberately, so state handling is testable without a credential. **The loop
that actually calls Graph and acts on results does not exist.**

**Permission:** `Mail.ReadBasic` is the least-privileged for message delta;
`Mail.ReadWrite` qualifies as higher-privileged and covers it (DOCUMENTED).

**Implementation, per the watch-brief's recommendation — delta polling, not webhooks:**

The watch-brief settles this before any code:

- A change-notification endpoint **must be publicly reachable HTTPS**, stated twice in
  consecutive paragraphs, and *"if your endpoint isn't publicly accessible, Microsoft
  Graph doesn't send notifications"* (DOCUMENTED). An MSP's small-business clients sit behind
  NAT with no inbound ports — the correct configuration for that customer, not a
  deficiency to engineer around. A push tier cannot run *at the client* at all.
- The push failure mode is silent and permanent. An endpoint whose responses exceed the
  allowance in >15% of a 10-minute window enters "drop" state, and *"dropped
  notifications can't be recovered"* — stated twice on that page. A delta poller that
  misses a round picks up more next round.
- Delta is not a degraded fallback. Microsoft frames it as the peer of webhooks:
  *"Delta query uses a pull model... while change notifications use a push model."*

Shape that follows from the briefs:

1. **Enumerate the folder tree once.** `GET /me/mailFolders?includeHiddenFolders=true`
   plus recursive `childFolders`. Graph exposes **no single call returning the whole
   tree** — the docs state the root call returns only children of the root and "each
   child folder must be traversed separately" (DOCUMENTED). `$expand=childFolders`
   flattens exactly one extra level (INFERRED — described in community write-ups, not
   the reference page), halving calls without eliminating recursion.
2. **Identify system folders by id, never by `displayName`.** There is no
   `wellKnownName` property on the v1.0 `mailFolder` resource; documented properties
   are exactly `childFolderCount`, `displayName`, `id`, `isHidden`, `parentFolderId`,
   `totalItemCount`, `unreadItemCount` (DOCUMENTED). Resolve each well-known alias to
   its real id once and compare ids. `displayName` is localized and unreliable.
3. **Backfill with `GET /me/messages`** — mailbox-wide, no folder iteration needed,
   `$select`-narrowed, excluding Sent/Drafts/Outbox by `parentFolderId`.
4. **Watch incrementally with per-folder `messages/delta`.** *There is no mailbox-wide
   delta.* `/me/messages/delta` **is not documented and does not exist** (DOCUMENTED).
   Coverage means N delta streams and N tokens, one per folder, plus
   `mailFolder: delta` to learn about folders the client creates later.
5. **Key the local store on `internetMessageId`**, never on folder. A rule-driven move
   then reduces to a `parentFolderId` change on a record already held, and the
   remove/add pair stops looking like a delete.
6. **Treat resync as ordinary control flow.** Outlook delta tokens have **no fixed
   TTL** — *"the upper limit isn't fixed; it's dependent on the size of the internal
   delta token cache"* (DOCUMENTED). Directory objects get a documented seven days;
   Outlook gets cache eviction. A token can die at any time, for reasons outside your
   control, with no warning. `410 Gone` and `syncStateNotFound` are routine.
7. **Idempotency is mandatory.** *"Your application must be prepared for replays"*
   (DOCUMENTED).

**Throttling headroom:** the global limit of 130,000 requests per 10 seconds per app is
not a constraint here. The **per-mailbox Outlook figures are UNVERIFIED** — the
watch-brief reports honestly that the Outlook section of the throttling-limits page did
not render on two fetch attempts, and that the commonly cited 10,000-per-10-minutes and
4-concurrent-batch numbers came only from Q&A threads. Do not quote them to a customer.
The design has orders of magnitude of headroom under any plausible value: a 60-second
delta poll is ~10 requests per 10 minutes.

**What it cannot do:**
- Cannot achieve sub-minute latency reliably. Documented push latency for `message` is
  under 1 minute average, 3 minutes maximum; a 60-second poll is comparable in practice,
  a 5-minute poll is not. If a client genuinely needs sub-minute reaction, make them
  state the requirement rather than assuming it.
- Cannot attribute a move to a rule vs. a human (DOCUMENTED as impossible at the API
  level). Two partial mitigations exist — correlate by `internetMessageId`, or a timing
  heuristic (server rules fire at delivery within seconds; humans file minutes-to-days
  later) — and the second is explicitly a heuristic that must not be load-bearing for
  anything that mutates the mailbox.
- N delta streams means N independent failure modes. A large business mailbox with a deep
  client-folder tree could be hundreds of folders, each with a token that can be evicted
  independently. That is real operational weight for an MSP across many tenants.

### 2.8 Onboard / profile a mailbox

**State: PROPOSED.** Not built.

**Permission:** `Mail.ReadWrite` (read-only in practice for the profiling pass).

**Implementation:** the tree walk of §2.7 step 1 produces this for free.
`totalItemCount` and `unreadItemCount` are properties on every folder in the
enumeration, and the docs recommend them explicitly over `$count`+`$filter` queries,
which "can incur significant latency" (DOCUMENTED). A coverage report costs **zero
extra calls** beyond the walk that has to happen anyway.

The rules-brief notes this is a genuinely good artifact to put in front of a prospect,
because it shows the firm its own mail distribution before anything has been changed.

**What it cannot do:** it cannot see the Online Archive (§2.1), and it cannot read the
client's inbox rules — the rule-parsing direction was cancelled and is out of scope.

### 2.9 Voice sampling

**State: WRITTEN.** `read_sent_sample` and `get_voice_routing` implemented; a
deterministic sales-routing rule reads existing categories on sent mail.

**Permission:** `Mail.ReadWrite` (Graph); Gmail per the scope split above.

**What it cannot do:** it samples sent mail to learn tone. It does not send anything,
and per §2.5 the Microsoft path cannot.

---

## 3. Coexistence matrix

This is the core of the document. For each thing a client may already run: what it
does, what we do that is **different** (not better — different), how the two coexist,
and what we must **not** do so as not to break it.

The organising principle is one sentence, and it is the same law that governs how this
business talks about people: **enhance what is already there, never replace it.** A
client's rules are a deterministic mechanism that lands their placement correctly 100%
of the time. Under the repo's determinism-first law that means the rules keep that job.
We supply judgment; their mechanism supplies placement.

---

### 3.1 Outlook Rules

**What it does.** Far more than most pitches assume. The `New-InboxRule` surface
carries 30+ conditions across sender, recipient, subject/body, headers, size,
attachments, importance, sensitivity, classification, category (`AnyCategory`,
`AssignedCategories`, **`BulkCategory`**), flag, date, and eleven `MessageTypeMatches`
values (AutomaticReply, AutomaticForward, Encrypted, Calendaring, CalendaringResponse,
PermissionControlled, Voicemail, Signed, ApprovalRequest, ReadReceipt,
NonDeliveryReport). Every condition has an `ExceptIf*` form. Actions include
`MoveToFolder`, `CopyToFolder`, `ApplyCategory`, `MarkAsRead`, `FlagMessage`,
`StopProcessingRules`, and more. Rules created via `New-InboxRule`/OWA run **server-side**
and fire whether or not Outlook is open. (All DOCUMENTED, audit-brief §1.)

**What we do that is different.** A rule matches strings, addresses, flags, sizes,
dates, and enumerated message types. It has no model of meaning, state, or obligation.
The audit-brief's exact framing, which is the only honest one: **rules are expressive
about form and blind about meaning.** We add a semantic judgment — chiefly the
Action/Waiting distinction — that no condition in that surface can express.

**How the two coexist.** Perfectly, in a categories-only design, because the two write
to orthogonal properties. Their rules own `parentFolderId`. We own `categories`. A firm
with fifteen years of rules and a client-numbered folder tree keeps every bit of it.
Better than neutral coexistence: our category is **rule-able by them** — `AnyCategory`
and `AssignedCategories` are real conditions, so the client can write their own rule
that acts on our classification. That is the determinism-first composition running in
the client's favour: we supply judgment, their rules supply mechanism.

**What we must NOT do:**

1. **Never programmatically write inbox rules into a client mailbox.** Microsoft Learn
   states on the `New-InboxRule` page that creating or modifying a rule via Exchange
   PowerShell **removes client-side rules disabled by Outlook, and outbound rules**
   (DOCUMENTED). Blowing away a partner's hand-built rules is, in the audit-brief's
   words, an MSP-relationship-ending event.
2. **Never consume the rules quota.** It is **256 KB total for all rules per mailbox**,
   valid range 32 KB–256 KB, and in Exchange Online it **cannot be increased** past
   256 KB — only decreased (DOCUMENTED). It is shared with everything the client has.
   Consuming it with generated sender lists breaks the firm's ability to write their own
   rules, with a confusing error and no obvious cause.
3. **Never cite "45 rules" or "100 rules" as a Microsoft limit.** Microsoft states
   *"There isn't a maximum number of rules that users can create"* — the limit is bytes,
   not count, and the quota applies only to enabled rules. The circulating count figures
   are derived estimates (UNVERIFIED). Citing a wrong number to an MSP is a credibility
   loss on a checkable fact.
4. **Never call rules primitive.** They are not. An IT-literate MSP will call it out and
   the whole pitch loses credibility.

**Open item:** the exhaustive current list of conditions/actions that force a rule
client-only is UNVERIFIED — the mechanism is documented, the complete enumeration is
spread across older Learn/archive pages.

---

### 3.2 Focused Inbox

**What it does.** Splits the inbox into Focused and Other tabs. It is **not a folder
taxonomy and it does not move mail out of the Inbox**. Signal: correspondent graph plus
bulk/machine-generated content detection — it inherited the machine-learned model that
drove Clutter. A two-way importance classifier, trained per user, with no exposed
reasoning.

**What we do that is different.** Two-way vs. nine-way, and reasoning vs. none. Focused
Inbox says important or not; it has no concept of *who owes whom*, no reason string, and
no persistent state anything else can read.

**How the two coexist.** They are separate surfaces: Focused/Other is a tab split on the
Inbox view; our category is a property on the message. Both can be on. A message can sit
in Focused and carry `rTech: Business-Waiting`, and the two statements do not contradict
— one says "you interact with this sender," the other says "the ball is not in your
court."

**The hazard the audit-brief names, and it deserves a stance:** *"If both are on, users
see two competing sorting systems and trust neither."* That is a UX risk, not an API
conflict. The right handling is to say at onboarding which surface the user should look
at for what, rather than to quietly assume Focused Inbox is off.

**What we must NOT do:**

1. **Never promise Focused Inbox is off, or will stay off.** Admin control is **not
   binding**: even after an admin disables or enables it via
   `Set-OrganizationConfig -FocusedInboxOn $false` or
   `Set-FocusedInbox -FocusedInboxOn $False`, *"users can still enable/disable it on
   their email clients"* (DOCUMENTED). An MSP cannot guarantee a consistent state across
   a firm. Say so rather than being caught by it.
2. **Never build a two-way important/unimportant split.** It is Focused Inbox's, it is
   free, and it is in every tenant.

---

### 3.3 Sweep

**What it does.** Sender-focused bulk cleanup as a standing recurring rule: keep only
the latest from a sender, delete anything older than N days, or move all mail from that
sender to a folder. **Sweep rules run once per day.**

**What we do that is different.** Sweep's only condition is *sender*. It expresses
nothing about content, intent, or state. It is a retention tool, not a triage tool.
We classify per message on meaning.

**How the two coexist.** No overlap in mechanism. Sweep acts on a sender across time;
we act on one message on arrival. Sweep may delete or move mail we categorised —
which is fine, because a category travels with the message.

**What we must NOT do:** **do not build bulk cleanup, unsubscribe management, or
retention sweeps.** Sweep exists, it is free, and the audit-brief lists this among the
things not to build.

---

### 3.4 Clean Up (Conversation Clean Up)

**What it does.** Deletes redundant messages within a thread — *"if a message is
completely contained within one of the replies, the previous message is deleted."* It
protects unread, categorized, flagged, and digitally signed mail.

**What we do that is different.** Clean Up is thread deduplication, purely structural,
and decides nothing about importance.

**How the two coexist.** Notably well, and in our favour: **Clean Up protects
categorized mail.** A message carrying our category is one Clean Up will not delete.
This is coexistence that actively helps, not merely avoids harm. *(The protection list
is DOCUMENTED in the audit-brief's Microsoft Support source; whether "categorized"
covers any category or only certain ones is not further specified — treat the
favourable reading as UNVERIFIED before selling it as a feature.)*

**What we must NOT do:** nothing specific. Worth knowing operationally that Clean Up is
a **classic-Outlook feature not yet at parity in new Outlook for Windows**, which
matters because these firms are heading to new Outlook.

---

### 3.5 Quick Steps

**What it does.** One-click macros bundling multiple actions (move + mark read + flag +
forward). **Manually triggered, never automatic.**

**What we do that is different.** We act unattended; Quick Steps accelerate a human who
has already decided. Zero classification value.

**How the two coexist.** This is the best-fitting native tool in the whole matrix, and
the audit-brief says so: Quick Steps are *"genuinely excellent as the human correction
surface."* When our classifier is wrong, the user needs a one-click path to fix it, and
that path already exists in their client and they already know how to use it. A Quick
Step can strip our category and apply another. **The correction UI is something we do
not need to build** — we should document how to configure one.

**What we must NOT do:** do not build a competing one-click action bar inside a
different surface. The user's correction gesture should live where they already work.

---

### 3.6 Existing Categories

**What it does.** Colored, multi-valued, per-mailbox labels. A message can carry many.
Rules can apply and read them. Categories are storage, not judgment — something still
has to decide which one applies.

**What we do that is different.** We are the something that decides. The audit-brief's
framing of the whole product thesis lands here: Microsoft ships **AI judgment without AI
filing** (Copilot flags but does not file) and **filing without judgment** (rules file
but cannot decide). Categories are the native storage layer that has never had a
judgment layer attached to it.

**How the two coexist.** Namespaced and additive. Our labels carry a distinguishing
prefix (`rTech: ...`) so they never collide with a client's existing category names,
and `categories` is multi-valued, so ours sits alongside theirs on the same message
rather than displacing it.

**What we must NOT do — this is the single highest-blast-radius item in the document:**

1. **Never PATCH `categories` without reading first.** The collection-replace behaviour
   is **INFERRED and UNVERIFIED** (rules-brief §4a). If it replaces, a bare write
   destroys every category the client assigned themselves — the exact class of failure
   this entire coexistence posture exists to prevent. Read-union-write, always. That
   implementation is correct whether the semantics turn out to be replace or merge, so
   there is no reason to wait on the test to adopt it.
2. **Never rename or delete a client's master categories.** `outlookCategory.displayName`
   must be unique in the master list and is **immutable after creation** (DOCUMENTED) —
   there is no rename, only delete-and-recreate, which would orphan every message
   carrying the old name.
3. **Never assume color availability.** There are exactly **25 preset colors**
   (`preset0`–`preset24`, plus `none`) across the entire master list (DOCUMENTED). A
   firm with an established taxonomy may already be using most of them. Colors need not
   be unique, so sharing one is acceptable — assume we may have to.

---

### 3.7 Microsoft Copilot in Outlook

Given its own section below (§4), because it is the coexistence case most likely to be
mishandled.

---

### 3.8 SaneBox and comparable third-party triage

**What it does.** SaneBox is an overlay on existing mail — not a client — that sorts
into SaneLater / SaneNews / etc. folders over IMAP. Roughly $7 / $12 / $36 per month by
tier. Its security page states: *"Your emails never leave your server. SaneBox never
takes possession of them, and only analyzes headers to determine what's important"* and
*"When optional features retrieve the body of email they are never stored."*

**What we do that is different.** SaneBox's advertised posture is **header-only**, and
that is a real technical constraint, not a marketing choice: the Action/Waiting
distinction requires the body, because who-owes-whom lives in the last speech act, not
in the headers. The audit-brief marks this the honest and defensible differentiator —
and, notably, marks SaneBox (not Superhuman) as the real competitor on security posture.

**How the two coexist.** SaneBox moves mail into its own folders. We write categories.
Same orthogonality as with the client's own rules — a message in `SaneLater` can carry
`rTech: Business-Waiting`. In a categories-only design there is no conflict.

**What we must NOT do:**

1. **Do not move mail out of SaneBox's folders.** SaneBox's training loop reads where
   the user puts things; a third party relocating mail corrupts its learning and the
   user blames whichever tool they notice last.
2. **Do not claim we are "safer" without verifying our own architecture.** The
   audit-brief flags this explicitly: the deployment-posture claim *"is only ours if the
   architecture actually holds it — verify before selling it."* It is an internal
   verification obligation, not a research finding.
3. **Do not position against Shortwave.** It is **Gmail/Google Workspace only, with no
   Outlook/M365 support as of mid-2026** — listing it as a competitor to an M365 buyer
   signals the research was not done.
4. **Do not assert compliance conclusions.** Any third-party AI triage for these buyers
   raises automated-processing-of-personal-data questions and data-residency questions.
   Which specific obligations bind a given US business is **UNVERIFIED** and per repo policy
   requires fresh sourcing.

*(Two competitor readings conflict and neither should be repeated as fact: Clean Email's
actual body-content handling — sources directly contradict each other, marked
UNVERIFIED in the audit-brief.)*

---

### 3.9 The client's own hand-built folder tree

**What it does.** Whatever the firm decided over fifteen years — client numbers, client
names, year buckets, arbitrary nesting with no documented depth cap.

**What we do that is different.** Nothing about placement. We do not have an opinion on
their tree and should not acquire one.

**How the two coexist.** The tree is read-only to us. We enumerate it (§2.7) to know
where mail lives so the watcher is not blind to rule-moved mail, and we report counts
back as a coverage artifact (§2.8). We write categories to messages wherever they sit.

**What we must NOT do:**

1. **Never create folders in a client's tree without explicit configuration.** Note that
   the shipped `file_message` path resolves a folder id and **may create the folder** as
   a side effect. In a categories-only posture that path is not exercised; if filing is
   ever enabled for a client, folder creation must be an explicit, consented setting.
2. **Never identify a folder by `displayName`.** It is localized. Resolve well-known
   aliases to ids once and compare ids (DOCUMENTED).
3. **Never treat a search folder as storage.** `mailSearchFolder` objects come back
   inline in the same collection tagged
   `"@odata.type": "#microsoft.graph.mailSearchFolder"` and are virtual views
   (DOCUMENTED). Filter them or double-count.
4. **Do not assume the tree is static.** Use `mailFolder: delta` to learn about folders
   the client creates later, rather than re-walking or going stale.

---

## 4. Copilot specifically

**Do not read this as a comparison.** Copilot is not a competitor to plan around; it is
a thing a given client may or may not have, and the product must be correct in both
cases. Two instructions govern everything below, both from the audit-brief:
**do not assume the client has Copilot, and do not assume they lack it** — at $30/seat
it is a real line item and the conversation differs completely between the two cases.

### What Copilot does in Outlook

- **Summarize** — thread summaries in the reading pane. GA.
- **Draft with Copilot / Coaching** — generation and tone feedback on outbound. GA.
- **Prioritize my inbox** — assigns **high / normal / low**, shows a written explanation
  of *why* a message was judged important, and accepts natural-language instructions
  ("emails from my manager", "emails related to project X").

### The verified fact the composition rests on

**Copilot Prioritize flags and sorts. It does not file.** Per Microsoft Support it
*"assigns a priority (high, low, normal)"* with visual indicators and lets the user
*"sort or filter your inbox based on Copilot's priorities."* **The emails remain in the
inbox.** There is no folder taxonomy, no move action, and no persistent state a
colleague or an MSP can audit. This is verified against Microsoft's own support page —
it is not a marketing framing.

### How the two compose

Copilot's judgment stays Copilot's. It runs on Microsoft's model, with the user's own
natural-language instructions, and produces a priority the user already trusts or does
not. **We do not read it, override it, contradict it, or attempt to reproduce it.**

Our addition is a different axis. Copilot answers *how important is this?* on a
three-value scale. We answer *whose court is the ball in, and is there anything to do?*
on the nine-folder taxonomy. A message can be Copilot-high and `rTech: Business-Waiting`
at the same time — urgent, and nonetheless not the owner's move. Those two statements do
not compete; they are different questions, and a client running both should get **more
signal, not a conflict.**

The mechanism keeps them non-interfering: Copilot writes to its own priority surface, we
write to `categories`. Neither reads the other's storage. Neither can clobber the other.

### What we must NOT do

1. **Do not build summarization, drafting, or tone coaching.** Copilot owns these, does
   them well, and a client with Copilot has already paid for them. The audit-brief calls
   this fight unwinnable and unnecessary.
2. **Do not claim AI mail prioritization as a novel capability.** Copilot does that —
   with explanations and natural-language customization. A Copilot-licensed buyer will
   know, and the claim is false. Lead with filing and workflow state, never with
   prioritization.
3. **Do not claim we are better than Copilot at anything.** The defensible statement is
   narrow and factual: Copilot does not file, and the Action/Waiting distinction is not
   something it attempts.
4. **Do not quote Copilot pricing as fact.** $30/user/month as an add-on to a qualifying
   M365 base licence is consistent across sources, but **every source reached was a
   third-party pricing guide, not Microsoft's own pricing page — UNVERIFIED.** Same for
   the reported Copilot Business standalone at ~$18–21 and Business Standard with Copilot
   at ~$23.50, and for the claim that Office 365 E3 customers must upgrade to Microsoft
   365 E3 first.
5. **Do not state Prioritize's availability as settled.** Microsoft's own support page
   says *"If you don't see the Prioritize option, try again in a few days. This feature is
   being rolled out gradually."* GA was scheduled late-June-2025 through late-December-2025
   (VERIFIED via message-center archive), **but a report dated 2026-02-04 says it was
   removed from Outlook for iOS and Android "in response to user feedback and costs"** —
   sourced to a message-center mirror, not Microsoft directly, and marked UNVERIFIED. That
   it is currently Windows/web-only is also UNVERIFIED, third-party. Confirm before
   repeating any of it to a client.

### The caveat stated against our own interest

The audit-brief records it and it belongs here rather than buried: a three-value priority
with a natural-language customization box is *"genuinely a large chunk of what a
nine-folder taxonomy delivers in user-perceived value. And Microsoft can close the filing
gap in one release."* The durable position is not the classification idea — it is the
deployment posture, the audit trail, and the MSP control plane.

---

## 5. Permission ladder

Ordered least to most. An MSP reads this section first, because a capability that
quietly needs a scarier consent screen is a deal-breaker discovered too late.

| Rung | Permission | Buys | Notes |
| --- | --- | --- | --- |
| 0 | *(none)* | Classification logic (§2.2), digest rendering from local ledger | No I/O. Testable without a credential. |
| 1 | `Mail.ReadBasic` | Message delta (§2.7) | Least-privileged for `messages/delta` (DOCUMENTED). Not requested — `Mail.ReadWrite` covers it. |
| 2 | `Mail.Read` | Read mail (§2.1), create a change-notification subscription | Documented permission for subscription creation. Listed here for completeness; not requested. |
| 3 | **`Mail.ReadWrite`** ← *held* | Everything above, plus categories write (§2.3), folder move (§2.4), flag/importance, reply drafts (§2.5) | The connector's entire request on Microsoft. One permission. |
| 4 | `MailboxSettings.Read` | Read the master category list | Not held. Would allow detecting a client's existing categories before writing ours. |
| 5 | `MailboxSettings.ReadWrite` | **Create named, colored master categories** (§2.3) | **Not held.** The one real gap. See below. |
| — | `Mail.Send` | Sending | **Deliberately never requested.** |

### What degrades without `MailboxSettings.ReadWrite`

Writing a category **onto a message** is `Mail.ReadWrite` and already covered. Creating
the category **in the master list** is `MailboxSettings.ReadWrite`, which the connector
does not hold.

Without it, the rules-brief's reading is that assignment still succeeds and the label
renders **grey**, because there is no master entry to source a color from. That is
**INFERRED on the assignment succeeding and UNVERIFIED on the color fallback** — no
Microsoft Learn page states it outright, and it needs a live test.

If that reading holds, the degradation path is genuinely useful for an MSP install:
**the feature works, it just looks worse.** Grey labels are cosmetically poorer, not
functionally broken, and filtering/searching/rule-matching on the category name are
unaffected.

The design consequence: provision master categories **once at setup** — one
`MailboxSettings.ReadWrite` operation per mailbox — then assign forever with
`Mail.ReadWrite` only. That makes the scarier consent a one-time install-time ask that
can be declined without losing the capability, rather than a standing requirement.

### The multi-tenant consent reality — price it into onboarding

This is a fixed cost of the MSP topology, not a choice between architectures:

- **Delegated permission reaches only the signed-in user's own mailbox.** You cannot use
  a delegated token to subscribe to, or read, another user's mailbox (DOCUMENTED).
- **The Outlook `*.Shared` permissions do not support change-notification
  subscriptions** (DOCUMENTED). The permission whose name sounds right for MSP delegated
  access is the one that does not work.
- Therefore an MSP watching many mailboxes across many tenants is pushed to
  **application permissions**, and **app-only permissions always require tenant
  administrator consent** (DOCUMENTED). *"If your application requests an app-only
  permission and a user tries to sign in, an error message is displayed saying the user
  isn't able to consent."*
- A tenant admin can **disable user consent entirely**. Businesses that handle confidential client data are exactly the
  population likely to have this locked down. **Design for admin-consent-only as the
  baseline, not the exception.**
- Consent is **revocable at any time** via Enterprise applications → Permissions.
- Multi-tenant token validation is stricter: validate that the `issuer` in published
  metadata matches the token's `iss` claim, **in addition to** the usual `tid` check.
  Skipping this is a documented multi-tenant hazard.
- App registration must be set to "Accounts in any organizational directory," and the
  **Application ID URI must be globally unique across all tenants** or setting the app
  multitenant fails. For an MSP serving business tenants,
  `https://login.microsoftonline.com/organizations` is the correct authority — it
  excludes consumer accounts (INFERRED from the documented distinction).

**This admin-consent cost is identical for the push tier and the app-permission pull
tier.** It is not a reason to prefer one trigger over the other.

### The scope posture survives the watch tier — bank this

Graph's subscription permission model asks for *read* and explicitly refuses
unnecessary write scope: *"Due to security restrictions, Microsoft Graph subscriptions
don't support write access permissions when only read access permissions are needed"*
(DOCUMENTED). Nothing in either the push or pull path requires, mentions, or touches
`Mail.Send`. The `Mail.ReadWrite`-and-not-`Mail.Send` posture holds under every design
in this document. Worst case the connector *adds* `Mail.Read`, which is **less**
privilege, not more.

**One open item:** that `Mail.ReadWrite` alone suffices to create a mail subscription is
**INFERRED** — the doc states the rule as a capability floor ("your app must have read
permissions for the resource") and `Mail.ReadWrite` is a strict superset of `Mail.Read`,
but no Microsoft page enumerates `Mail.ReadWrite` in that table cell. One dev-tenant
call settles it.

### A standing constraint that must not be flipped quietly

**`includeResourceData: false`.** Basic change notifications carry **no message
content** — an id, a resource path, an etag, a tenant id, and the app's own
`clientState` echoed back. No subject, no body, no sender, no attachment (DOCUMENTED).

Rich notifications *do* carry content and are strictly opt-in: they require
`includeResourceData: true`, `encryptionCertificate`, and `encryptionCertificateId` set
explicitly at subscription creation. There is no path by which a connector accidentally
starts receiving message bodies.

If anyone ever proposes flipping it "to save an API call," that is a **security-posture
change requiring the buyer's explicit sign-off**, not an optimization. It would put
encrypted-but-complete message bodies onto the wire to the notification endpoint, add
private-key custody as a new obligation, and cut subscription lifetime from ~7 days
(10,080 minutes) to ~1 day (1,440 minutes).

---

## 6. What we will not build, and why

Naming these protects credibility with a technical buyer far more than claiming them
would gain. Every item is sourced to the audit-brief, which was commissioned to be
unflattering and was.

### 6.1 Newsletter detection

**Largely solved natively.** `HeaderContainsWords "List-Unsubscribe"` plus Exchange's
`BulkCategory` condition plus Focused Inbox's own newsletter/machine-generated detection
already handle this well. The audit-brief scores Newsletters as the one folder in the
nine where a rule gets a **YES**, and says plainly: *"Be honest: this is close to solved
natively... Charging for AI newsletter detection is close to selling a rule."*

The residual — solicited vs. unsolicited — is genuinely undecidable from the message
alone (§2.2) and is not claimed.

### 6.2 Summarization

**Copilot's, done well, already paid for by any client who has it.** Building it is
redundant spend and an unwinnable fight.

### 6.3 Drafting and tone coaching

Same. Note this is a scope boundary, not a capability boundary — the connector *can*
create reply drafts (§2.5), because the workflow needs a place for the classification to
land as an action. It does not attempt to be a better writing assistant than Copilot.

### 6.4 Bulk cleanup, unsubscribe management, retention sweeps

**Sweep and Clean Up exist and are free** (§3.3, §3.4). Do not build sender-scoped bulk
deletion.

### 6.5 A two-way important/unimportant split

**Focused Inbox's**, free, in every tenant (§3.2).

### 6.6 Priority scoring as a headline capability

**Copilot's** — including the natural-language customization box (§4). We may produce a
classification that correlates with priority; we do not sell prioritization.

### 6.7 Ongoing AI for Not-Interested senders

Once a sender is known unwanted, **a rule handles it perfectly and forever** — the
audit-brief says a rule is arguably *better* here: deterministic, auditable, free. The
AI value is only in the *first* classification. Under the repo's determinism-first law
the correct design is to **emit a deterministic rule and stop spending inference** —
subject to §3.1's absolute prohibition on writing rules programmatically, which means
emitting a rule the client applies, not one we install.

### 6.8 Rule reading, parsing, prediction, or generation

Cancelled and out of scope (rules-brief, controller correction). Reading rules needs
`MailboxSettings.Read`; writing them needs `MailboxSettings.ReadWrite` and carries the
client-side-rule-destruction hazard (§3.1). The coexistence posture makes the whole
direction unnecessary: if we never move mail, we never need to predict where their rules
will.

### 6.9 A webhook / push tier, for now

Deferred, not refused — see §2.7. It requires either a public HTTPS endpoint the MSP
hosts (a new internet-facing attack surface in the delivery path of confidential client
mail metadata) or Azure Event Hubs (an Azure subscription, a broker, per-tenant
plumbing). Delta work is not thrown away when push arrives: Microsoft's own recommended
architecture is push-as-trigger, delta-as-state, so the Guardian tier becomes a latency
optimization firing the same delta round, not a second implementation.

### 6.10 Claiming Outlook rules are primitive

Not a feature decision — a credibility one, and the audit-brief ranks it high. Rules
carry 30+ conditions, eleven message types, bulk classification, sensitivity, and
classification. **The correct framing is narrow and true: rules are expressive about
form and blind about meaning.**

---

## 7. Open items requiring verification before any client tenant

Consolidated. Nothing below should be stated to a client as fact until it is checked.

**Ours to verify internally:**

1. **`categories` PATCH replacement semantics** — does it clobber existing categories?
   INFERRED/UNVERIFIED, touches client data, must not be guessed at. *(A live test was
   reported in progress at the time of writing; its result is not recorded here.)*
   Implement read-union-write regardless.
2. **Category visibility on Outlook mobile** — UNVERIFIED. Five-minute check on a real
   device. The value proposition depends on it.
3. **Whether `/me/messages` actually returns Junk Email** (and Sent/Drafts/Outbox) —
   INFERRED, UNVERIFIED.
4. **Whether `Mail.ReadWrite` alone creates a mail subscription and runs a delta query** —
   INFERRED. One dev-tenant call each.
5. **Whether the architecture genuinely keeps message bodies in-tenant** — the
   deployment-posture claim against SaneBox/Superhuman depends entirely on this and it is
   ours to verify internally, not by research.
6. **Grey-label degradation without `MailboxSettings.ReadWrite`** — INFERRED on
   assignment succeeding, UNVERIFIED on the color fallback.
7. Whether **Clean Up's protection of "categorized" mail** covers any category — the
   favourable reading in §3.4 is UNVERIFIED.

**Research items still open:**

8. **Copilot pricing** against a Microsoft-owned URL. All sources reached were
   third-party.
9. **Whether Prioritize my inbox is GA** as of 2026-08, and the reported 2026-02-04
   removal from iOS/Android (single mirrored source), and whether it remains Windows/web
   only.
10. **Per-mailbox Outlook throttling figures.** The reference table's Outlook section did
    not render on two fetch attempts; the circulating 10,000-per-10-minutes and
    4-concurrent-batch numbers came from Q&A threads only. Do not quote them.
11. **Exhaustive current client-only rule condition/action list.**
12. **Clean Email's actual body-content handling** — sources directly contradict each
    other.
13. **Whether any v1.0 path reaches the Exchange Online Archive mailbox.** The beta
    `/admin/exchange/mailboxes` surface is the only lead.
14. **Compliance obligations** (GDPR/UK GDPR analogues, data residency, ethics,
    malpractice) — not researched to the required standard. Per repo policy these need
    fresh sourcing and must never be asserted as verified.
