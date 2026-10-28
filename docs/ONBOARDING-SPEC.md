# Inbox Zero — Interactive `setup` Onboarding Spec

**Status: PROPOSED. Nothing in this document is built.** No `setup`
subcommand exists in `connector/src/inbox_zero_connector/cli.py` as of
2026-08-18; the shipped command surface is `serve`, `auth`, `init-taxonomy`,
`init-config`, `show-scopes`, `paths`, `digest`, `logout`. Every config field
this document proposes to write is new unless it is explicitly named as an
existing field, and each new field is marked NEW at its first mention. This is
a design document to be argued with, not a description of behaviour.

Spec version: 0.1 (draft against connector 0.2.0, kit VERSION 0.2.0).

---

## 0. The problem this solves

Installing the connector today is: run `auth`, approve a consent screen, done.
The operator's observation, verbatim:

> "right now it's just approve. There's nothing in there. Just approve. I
> think that the app could actually ask some questions at that point."

That is accurate. `_cmd_auth` (`cli.py:47`) prints the scope manifest, runs
the OAuth flow, and stores a credential. It learns nothing about the mailbox.
`init-config` (`cli.py:97`) takes an owner domain and two boolean flags off the
command line. `init-taxonomy` (`cli.py:135`) writes nine hardcoded folder names
from `taxonomy.py:28` with no reference to what the mailbox already contains.

The consequence: **every install is byte-identical and none is tuned.** A
40-attorney firm with an existing `Action/`, `Waiting/`, `Clients/` structure
gets nine new folders bolted alongside the ones they already use, and the
classifier files into the new ones while the humans keep looking in the old
ones. That is not a configuration gap, it is the failure mode that makes an
owner stop trusting the system — the same failure `Review` exists to prevent
at the message level (`taxonomy.py:89`), reproduced at the install level.

### Who is at the keyboard

The buyer is an MSP serving law firms and financial agencies. **The person
running `setup` is frequently an MSP technician, not the mailbox owner.** This
single fact constrains the entire question set:

- A technician can answer *structural* questions (which folders exist, what the
  firm's mail domain is, whether this is a shared or personal mailbox, who
  staffs the triage queue) because those are facts about the deployment.
- A technician **cannot** answer *preference* questions about someone else's
  working habits ("do you want newsletters filed or flagged?", "how formal is
  your writing?"), and asking them produces a confident wrong answer that
  nobody ever revisits.

Every question below is therefore tested against: *can a competent technician
who has never met the mailbox owner answer this correctly?* If no, the question
either becomes structural, gets a safe default and a deferral, or does not get
asked. §6 lists the ones that do not get asked.

### What the DETERMINISM-FIRST law requires here

`AGENTS.md` (Rico ruling 2026-08-11): anything a script can land 100% of the
time, the script owns. Applied to an interview, this cuts in two directions and
both matter:

1. **Do not ask what we can detect.** The mailbox's folder tree, the owner's
   mail domain, the provider, the message volume, whether folders named
   `Action`/`Waiting` already exist, which categories are in use, which senders
   the owner has never once replied to — all of these are reads away. An
   interview that asks for a fact it could have read is a defect, and it is a
   defect that costs trust at the exact moment we are asking for trust. §1 is
   the pass that reads them.
2. **Do not ask the AI what the interview should be.** The question set is a
   fixed table in code, the answers are validated deterministically, and the
   resulting config is a file a human can read. No model participates in
   `setup`. The dry-run in §4 is the only place a model runs at all, and its
   output is labelled as a sample, never as a decision.

### 0.1 The shape: profile first, then confirm

The interview does not open with blank questions. **A read-only profiling pass
runs first (§1), and every question it can pre-answer arrives as a proposal the
human confirms or overrides (§3).** "You have 34 folders; these 6 look like
matter files — exclude them?" is a question a technician can answer in two
seconds. "Which folders should be excluded?" is one they cannot answer at all.

This is the determinism law applied to the interview itself: the mailbox knows
most of these answers, so a script reads them and the human adjudicates the
handful that are genuinely judgement.

### 0.2 Rejected: the self-configuring email

The operator floated having the connector **send itself an initial email** that
routes to a configurator. **This spec does not propose a send path, and the
reason is not caution — it is that sending is the one thing this product sells
by not doing.**

The connector holds `Mail.ReadWrite` and deliberately not `Mail.Send`. On
Microsoft that makes the no-send guarantee **scope-layer**: Microsoft's own
permission layer refuses a send made with this token, and the buyer can verify
it from their own admin console without trusting a line of our code
(`cli.py:77`, `_cmd_show_scopes` at `cli.py:149`). `providers/base.py` states
the design plainly — send, delete, and forward are not blocked or feature-
flagged, they are **not implemented**, and `tests/test_security_surface.py`
fails the build if a method appears.

The buyer is an MSP that specifically asked whether this connector can
exfiltrate data. Today the answer is a structural no. A self-configuration
email would change it to "it can send, but only to itself, and only during
setup" — a sentence containing three qualifiers where there used to be none.
Concretely, it would cost:

- `Mail.Send` on the consent screen, which is the screen the security answer is
  built on. Every subsequent conversation starts from "why does a read-and-file
  tool need send?"
- The scope-layer guarantee, permanently. A scope is granted per token, not per
  purpose; a token that can send during setup can send afterwards.
- An eighth capability on the provider interface, through the security-surface
  review `providers/base.py` exists to force.

**Against, at the cost of a slightly better setup UX.** The profiling pass in
§1 delivers what the email was reaching for — an install that configures itself
from the mailbox — using reads the connector is already permitted to make. If
the self-configuration goal ever genuinely requires a send, that is a
product-posture decision for Rico and the MSP, not an implementation detail of
an installer, and it should be argued on its own rather than arriving inside an
onboarding change.

---

## 1. The profiling pass

**Read-only. Writes nothing. Sends nothing. Runs before the first prompt.**

The profiler's job is to turn blank questions into confirmable proposals. It is
a script — no model participates, every inference is a stated heuristic over
counts and dates, and every conclusion it reaches is a **proposal**, never an
applied setting.

### 1.1 Exactly what it reads

Named endpoint by endpoint, because "read-only" is a claim a buyer will want
itemised. Graph shown; the Gmail equivalents are flagged in §7.

| # | Read | Purpose | Verb |
| --- | --- | --- | --- |
| P1 | `GET /me/mailFolders` (+ child traversal) | Folder tree, per-folder counts, last activity | GET |
| P2 | `GET /me/outlook/masterCategories` | The category vocabulary the client already defined | GET |
| P3 | `GET /me/mailFolders/inbox/messages` (headers only, `$select`) | Volume, unread ratio, top senders | GET |
| P4 | `GET /me/mailFolders/sentitems/messages` (recipients only, `$select`) | Reply history — has the owner ever written to this sender? | GET |

**Every verb in that table is GET.** The profiler issues no POST, no PATCH, no
PUT, no DELETE, and no `/sendMail`. It does not call `file_message`,
`set_important`, or `create_draft`. It creates no folder. It writes no ledger
row (`ledger.py` receipts mutations, and the profiler performs none). It writes
nothing to the config directory — the profile is held in memory for the
duration of `setup` and §1.5 governs what, if anything, survives.

As with the dry-run (§4.1), this should be enforced structurally rather than by
discipline: the profiler runs against a **read-only provider wrapper whose
mutating methods raise**, so a profiler that accidentally reaches `file_message`
crashes instead of filing.

**One capability constraint, from the existing code.** The sent-mail read (P4)
is not an unbounded scrape and cannot become one: `SentSelection`
(`providers/base.py:157`) refuses a selection with no labels, no ids, and no
date bounds, precisely so that "learn about my mail" cannot mean "read
everything the owner ever wrote". The profiler therefore passes an **explicit
date bound** (§1.2), and it reads a narrower `$select` than the voice profile
does — recipients and dates only, no bodies. Whether that narrower projection
needs its own method on the provider interface, rather than reusing
`read_sent_sample`, is a security-surface question for the boundary owner, not
this spec's call (§7 #7).

### 1.2 Bounded cost, and how it degrades

A 200,000-message mailbox must not turn setup into an hour-long job. The
ceilings are constants, named so they can be argued with:

| Constant | Value | What it bounds |
| --- | --- | --- |
| `PROFILE_INBOX_SAMPLE` | 500 | Inbox headers read for volume, unread ratio, and sender ranking |
| `PROFILE_SENT_SAMPLE` | 500 | Sent headers read for reply history |
| `PROFILE_SENT_DAYS` | 365 | Date bound on the sent read; satisfies `SentSelection` |
| `PROFILE_MAX_FOLDERS` | 200 | Folders enumerated before the tree pass degrades |
| `PROFILE_MAX_REQUESTS` | 40 | Hard ceiling on total HTTP requests for the whole pass |
| `PROFILE_TIMEOUT_SECONDS` | 60 | Wall-clock ceiling |

**Cost at those ceilings:** roughly 3–8 requests for the tree, 1 for master
categories, 3 for the inbox sample (500 at `MAX_PAGE_SIZE = 200`,
`providers/outlook.py:77`), 3 for the sent sample. Call it **10–15 requests and
under 30 seconds** on a typical mailbox — a claim that is PROPOSED, not
measured, and that §7 #1 names as needing a live run to settle.

**Degradation is explicit and reported, never silent.** Hitting a ceiling
prints a line in the profile summary:

```
NOTE: this mailbox has 8,412 Inbox messages; the profile read the 500 most
recent. Sender rankings and the unread ratio describe that sample, not the
whole mailbox.
```

- **Over `PROFILE_MAX_FOLDERS`:** stop enumerating, report
  `340 folders (enumeration stopped at 200)`, and make no exclusion proposals
  at all — a proposal derived from a partial tree is a guess wearing a
  proposal's clothing. Q1's default drops to Inbox-only.
- **Over `PROFILE_MAX_REQUESTS` or `PROFILE_TIMEOUT_SECONDS`:** stop, report
  which sections are incomplete, and **fall back to a cold interview for the
  affected questions**. A partial profile never produces a proposal.
- **On any read error:** report it, degrade that section, continue. The
  profiler failing must never block the install; it is an accelerator, not a
  precondition.
- **`--no-profile`** skips the pass entirely and runs the cold interview from
  §3's defaults. This is the escape hatch for a client who will not authorise a
  bulk read before they trust the tool, and it must keep working.

### 1.3 What it infers, and the heuristics stated honestly

**Every heuristic below can be wrong, and every one produces a proposal a human
overrides.** They are string and arithmetic rules over folder names, counts, and
dates. No model runs. They are stated here so a reader can judge them rather
than discover them.

**H1 — Folders that look like an existing action/waiting/reference system
(propose MAPPING onto, per Q4).**
A top-level folder whose name case-insensitively matches or contains one of a
fixed keyword set: `action`, `todo`, `to do`, `waiting`, `pending`, `follow up`,
`followup`, `reference`, `ref`, `reading`, `receipts`, `invoices`, `billing`,
`newsletters`, `subscriptions`, `junk`/`spam` handled separately.
*Wrong when:* a firm has a client named "Action Plumbing", or uses `Billing` as
a matter folder rather than a receipts folder.
*Confidence shown:* the folder's message count and last activity are printed
beside the proposal, which is usually enough for a human to see the error.

**H2 — Folders that look like matter files or archives (propose EXCLUDING).**
A top-level folder is proposed for exclusion when **any** of:
- it has **more than 5 subfolders** and its subfolder names are mostly distinct
  proper nouns or contain digits (matter-number shape: `2024-0412`,
  `Vance-Carter`) — the client-file pattern;
- its name matches `archive`, `old`, `20\d\d`, `closed`, `former`;
- its **last activity is older than 180 days** while its message count is
  greater than 100 — a settled store, not a working folder.
*Wrong when:* a firm keeps active matters in a folder that looks archival, or
files current work under a year name.
*Presentation:* proposed for exclusion, shown with counts and last-activity
dates, one keystroke to keep any of them. **Exclusion is the narrower outcome,
which is why a wrong guess here is cheap** — the connector touches less mail
than it could have, and the fix is a re-run (§5).

**H3 — Existing category vocabulary (propose EXTENDING, not replacing).**
From P2 plus categories actually observed on sampled messages. A client already
labelling their mail has a vocabulary that beats ours *for them*, and the
proposal is to map our nine concepts onto their labels the same way Q4 maps onto
their folders. **A client with categories in use is a client we extend, not one
we overwrite** — which is the same instinct behind mapping onto `Action/` rather
than creating `Business-Action` beside it.
*Wrong when:* the master list is full of stale categories nobody uses. Mitigated
by printing an in-use count beside each: `Client (used on 143 of 500 sampled)`
vs `Q3-Push (used on 0 of 500 sampled)`.

**H4 — Reply history: has the owner ever written to this sender?**
The strongest signal available, and worth its cost. Computed as:
1. From P3, rank Inbox senders by volume; keep the **top 25**.
2. From P4, collect the **set of recipient addresses** across the sent sample —
   `toRecipients` and `ccRecipients` only, no bodies, no subjects.
3. For each of the 25, `has_replied = sender_address ∈ recipient_set`.

Set membership over two already-fetched samples — **zero additional API calls
beyond P3 and P4**, which is what makes it affordable. It separates Action from
Not-Interested better than any content heuristic can, and it does so without
reading a single word of any message: a sender the owner has written to 40
times is a human relationship, and a sender of 200 messages the owner has never
once answered is a mailing list, regardless of how urgent the subject lines
look. It is also **injection-proof by construction** — an address either is or
is not in the sent set, and no text in a hostile email can change that. Same
property `classify.py` relies on for its rule pass.
*Wrong when:* the owner replies by phone; a new but important sender has no
history yet. So it **never files anything by itself** — it feeds Q8's
proposals, and a no-reply sender is proposed for `Not-Interested`, never
assigned to it.
*Bounded:* top 25 senders, from a 500-message sample, over 365 days.

**H5 — Volume and distribution, which changes what the product should do.**
Messages/day (from the sample's date span), unread ratio, and the ratio of mail
sitting in Inbox to mail already filed elsewhere. Three shapes, each changing
what setup recommends:
- **Drowning** — >4,000 in Inbox, or unread ratio >40%. Recommend folder moves
  and a first full sweep; the value is immediate and visible.
- **Already tidy** — <200 in Inbox, most mail filed. Recommend **categories**
  and say plainly that the sweep will do little: this client already has a
  system and the product's job is to keep it, not to prove itself.
- **Mixed** — anything else. Defaults stand, no recommendation printed.

This one changes the *recommendation*, never the default silently, and the
reasoning is shown: `Recommended: folder moves (8,412 in Inbox, 61% unread).`

### 1.4 Presentation: proposals, never decisions

The exact contract, because "it's only a proposal" is worth nothing if the
presentation reads like a fait accompli:

1. **The profile summary prints before the first question**, as findings.
2. **Every proposal shows the evidence that produced it** — the count, the
   date, the matched keyword. A technician who disagrees can see why the
   profiler thought so.
3. **Every proposal is a prompt with a default, not a notification.** The
   proposal sets the *default*; it never sets the value.
4. **Overriding is one keystroke**, and the override path is on screen. Never
   "re-run with a flag".
5. **A proposal is visibly a proposal.** The word `Proposed` appears on the
   line. Applied settings are only ever printed after the §4 confirmation.
6. **Low-confidence proposals are labelled and default to the safe side.**
   H2 exclusions on folders with recent activity print
   `(recent activity -- less certain)` and default to *keep*.
7. **Nothing the profiler concludes is written anywhere before §4's `apply`.**

### 1.5 Privacy: what is retained, and where

The buyer's objection is storage and exfiltration. The answer must be
specific, not reassuring.

**The profiler retains DERIVED FACTS ONLY. It never retains message bodies or
subject lines.**

| Retained in memory during setup | Never retained |
| --- | --- |
| Folder names, message counts, last-activity dates | Message bodies |
| Category names and in-use counts | Subject lines |
| Sender email addresses (top 25) and a `has_replied` boolean | Message ids |
| Aggregate counts, ratios, date spans | Attachments, recipients beyond the boolean |

**Where it goes after `setup` exits:**

- **The in-memory profile is discarded.** Nothing is cached to disk between
  runs; a re-run re-profiles.
- **`connector-config.json` and `taxonomy.json` receive only the CONFIRMED
  answers** — folder names, mode strings, booleans. The profile's raw findings
  are not persisted. A config file is a document the client may read; it should
  contain their decisions, not our observations about their mail.
- **`--print-answers` (§2.2) emits confirmed answers only.** It contains folder
  names, which are client information, and the Review owner's name. It contains
  **no sender addresses, no counts, no subjects.** An MSP can hand that file to
  a colleague without handing over a mailbox summary.
- **Sender addresses never leave the process.** They rank Q8's proposals and
  are dropped. They are not written to the config, the answer file, or the
  ledger — matching `ledger.py:20`, which stores no addresses for exactly this
  reason.

**One deliberate exception, stated rather than hidden:** subject lines appear
in the §4 dry-run's five sample classifications, truncated to 60 characters, so
a human can sanity-check that the classifier is not obviously wrong. They are
printed to the terminal and **never written to a file**. §4.3 rule 3 governs
it, and a technician pasting a dry-run into a ticket should redact them. If the
buyer objects, `--no-samples` should suppress that block; the dry-run is still
readable without it.

**What is logged.** The profiler emits counts and folder names at INFO, and no
sender addresses or subjects at any level. `logging_config`'s redaction table
already covers credentials; sender addresses are a new category to add to it
before this ships (§7 #8).

### 1.6 Non-interactive profiling for the 40-mailbox rollout

A technician deploying to 40 mailboxes needs profile-and-apply in one command:

```
inbox-zero-connector setup --answers firm.json --accept-proposals=safe
```

`--accept-proposals` takes a **blanket policy**, not a blanket yes:

| Value | Accepts unattended | Refuses |
| --- | --- | --- |
| `none` (default) | nothing — proposals need a human | everything |
| `safe` | **narrowing** proposals only: H2 exclusions, H5's recommendation, category vocabulary observation | anything that widens scope or creates folders beyond the answer file's mapping |
| `mapping` | `safe`, plus H1/H3 mappings onto existing folders and categories | scope widening; anything §1.6.1 refuses |

**Why `safe` accepts only narrowing.** An unattended wrong answer that makes
the connector touch *less* mail is a re-run. An unattended wrong answer that
makes it touch *more* is mail moved in a mailbox nobody was watching. The
asymmetry sets the policy, the same way it sets every default in §3.

#### 1.6.1 What `--accept-proposals` must still refuse, at every level

These are refused even at `mapping`, and the run exits non-zero naming the
mailbox:

1. **Q6, who reads Review.** No profiler can infer a staffing assignment. An
   unattended run with no `review_owner` in the answer file writes
   `unassigned` and emits the §3 Q6 warning — it does not guess a name.
2. **Q7, the drafts posture.** A security posture is never inferred. It comes
   from the answer file or it is off.
3. **Any proposal that would WIDEN scope** beyond the answer file — including
   H2 declining to exclude a folder. Unattended widening is refused; the
   narrower answer stands.
4. **A degraded profile (§1.2).** If a ceiling was hit or a read failed,
   proposals from the affected section are refused unattended. A partial
   profile plus a blanket accept is exactly how 40 mailboxes get silently
   misconfigured.
5. **A mailbox whose profile differs materially from the answer file's
   assumptions** — the answer file maps `Business-Action -> Action` and this
   mailbox has no `Action` folder. Refuse, name the mailbox, exit non-zero.
   Falling back to "create it" would mean one mailbox in 40 silently getting a
   different structure, discovered months later.

Refusals accumulate and print as a list at the end of a batch, so a technician
gets one pass and a short list of mailboxes needing a human, rather than a
failure per run.

---

## 2. Command surface

### 2.1 Recommendation: `setup` WRAPS `init-config` and `init-taxonomy`, and RUNS AFTER `auth`

The proposed surface:

```
inbox-zero-connector auth microsoft      # unchanged. OAuth only.
inbox-zero-connector setup               # NEW. The interview. Requires a credential.
inbox-zero-connector init-config ...     # unchanged. Stays. Non-interactive primitive.
inbox-zero-connector init-taxonomy ...   # unchanged. Stays. Non-interactive primitive.
```

`setup` is a **wrapper that composes the two existing writers**, not a
replacement for them and not a phase inside `auth`.

**Argument for wrapping rather than replacing.** `init-config` and
`init-taxonomy` are the deterministic writers: they take validated values and
produce `connector-config.json` and `taxonomy.json`. Their internals
(`write_config` at `config.py:763`, `write_taxonomy` at `taxonomy.py:265`) are
already the single sanctioned path to those files, and both are deliberately
unreachable from any MCP tool. Making `setup` write those files by a second
route would create a second place the file shape can drift, and the shape
includes `drafts_enabled`, which `config.py:612` treats as a security posture
with a startup-time contradiction check. One writer, one shape. `setup`
collects answers; the existing functions serialise them.

Keeping the primitives also preserves a real workflow: an MSP with a
configuration-management system wants to drop a known-good
`connector-config.json` on 40 machines and never run an interview at all. That
is the correct thing for them to do, and removing `init-config` would break it.

**Argument against folding `setup` into `auth`.** `auth` is the security
ceremony. It prints the exact scope list, prints what is *not* requested
(`cli.py:77`), and prints the Gmail code-layer warning (`cli.py:68`). That
output is evidence in a security questionnaire, and its value comes from being
short enough to read in full. Appending an eight-question interview to it
buries the scope manifest in the scrollback of a wizard. They are also
differently re-runnable: `auth` is re-run when a token expires, `setup` is
re-run when the firm reorganises its folders, and those events have nothing to
do with each other.

**Argument against `setup` replacing `auth`.** The interview needs a live
mailbox — §3's folder-scope question is worthless without the actual tree, and
§4's dry-run requires reading real mail. So `setup` depends on a credential
existing. Making `setup` also *acquire* the credential would mean the interview
owns the OAuth flow, and the consent-screen text would then be printed by a
command whose primary job is something else. Dependency, not merger:

```
setup: if no stored credential for the configured provider ->
       exit 2, print exactly:
       "No stored credential for microsoft. Run
        'inbox-zero-connector auth microsoft' first, then re-run setup."
```

Exit 2 (not 1) so an MSP script can distinguish "you skipped a step" from "an
answer was invalid".

### 2.2 Non-interactive mode: an answer file, plus flag overrides

An MSP deploying to 40 mailboxes cannot answer 8 prompts 40 times. The
mechanism is an **answer file**, with flags as a per-mailbox override layer.

```
inbox-zero-connector setup                              # interactive
inbox-zero-connector setup --answers firm-standard.json # non-interactive
inbox-zero-connector setup --answers firm-standard.json --owner-domain other.example
inbox-zero-connector setup --answers firm-standard.json --dry-run-only
inbox-zero-connector setup --print-answers              # NEW: emit the answer file this install used
```

**Why a file rather than flags alone.** Eight questions is eight flags, several
of them repeatable lists (excluded folders, taxonomy mappings). A 40-mailbox
rollout with that shape is a 300-character command line per mailbox, generated
by a shell loop, unreviewable and undiffable. A JSON answer file is a
reviewable artifact the MSP can put in their own config management, diff
between clients, and show a client as "this is what we configured for you". It
is also symmetric with what the connector already does: the config layer is a
JSON file (`config.py:426`), and the answer file is the *input* to producing
one.

**Why flags on top rather than a file only.** Exactly one field varies per
mailbox in a typical rollout — the mailbox identity, and sometimes the owner
domain. Requiring 40 near-identical files to differ in one key is worse than a
single file plus one flag.

**Precedence, stated as a chain so it cannot be ambiguous:**

```
1. explicit command-line flag           (highest)
2. --answers file value
3. detected value                       (folder tree, provider, owner domain)
4. spec default                         (lowest)
```

This deliberately mirrors, but does not extend, the existing settings
precedence documented at `config.py:19`. `setup` runs *before* that chain
applies: it produces the JSON layer that sits at the bottom of it. `setup`
must not read `INBOX_ZERO_*` environment variables to answer its own questions;
doing so would let an environment variable silently decide what gets written to
the file, which is the exact inversion `config.py:612` refuses for
`drafts_enabled`.

**`--print-answers`** exists so that an interactive install can be *captured*
into an answer file for the other 39 mailboxes. Run the interview once with a
human on the first mailbox, review the dry-run, then
`setup --print-answers > firm-standard.json` and deploy that. This is the
determinism law applied to onboarding itself: a human figures out the path
once, then it is frozen into an artifact.

### 2.3 What happens when a question is skipped

Every question has a stated default (§3). "Skipped" has three distinct causes
and they are handled differently:

| Cause | Behaviour |
| --- | --- |
| Interactive, user presses Enter | Default applies. The chosen value is echoed back on the confirmation screen, so a default is never invisible. |
| `--answers` file omits the key | Default applies, and the line `DEFAULTED: <field> = <value>` is printed to stderr. Defaults in a non-interactive run must be loud, because nobody is watching the prompt. |
| Question is not answerable for this deployment (e.g. business/personal split on a shared mailbox) | The question is **not asked at all**, and the field is written with its structural value. See §3, Q5. |

**Every default in this spec is the narrower behaviour.** Excluded rather than
included, label rather than move, keep the client's folders rather than create
ours, defer rather than assume. A default that widens what the connector
touches on a mailbox nobody interviewed is the same class of defect as
`default_config()` defaulting drafts to false (`config.py:382`) — and that
precedent is already set in this codebase, so this is consistency, not a new
principle.

### 2.4 Ordering guarantee

`setup` writes nothing until §4's confirmation. The interview collects answers
into an in-memory structure; the dry-run renders from that structure; the write
phase runs only after an explicit confirmation. A `SIGINT` at any point before
confirmation leaves the mailbox and the config directory exactly as they were.

---

## 3. The question set

Nine questions, of which Q9 is asked only when the profile supports it. Q0 is
the profile summary rather than a question, but it is listed because it is what
makes every question after it answerable.

**Each question below states what the profiler pre-answers.** Where the profile
produced a finding, the prompt arrives as a **proposal with the profiler's
answer as the default and the evidence printed beside it** (§1.4). Where the
profile is absent, degraded, or the question is not the kind a mailbox can
answer, the prompt falls back to the cold form and the stated default. Both
forms are given for the questions where they differ.

Notation: **writes** names the config field. Fields marked NEW do not exist in
`config.py` today and are part of this proposal.

---

### Q0 — Profile summary (asked of the mailbox, not the human)

Before the first prompt, the §1 profiling pass runs and prints its findings:

```
Mailbox      : jvance@vancecarter.example  (Microsoft 365)
Owner domain : vancecarter.example         (detected from the authenticated account)
Folders      : 34 top-level, 61 total
Messages     : 8,412 in Inbox, 61% unread, about 47/day
Shape        : drowning -- folder moves recommended (H5)
Categories   : 6 defined, 3 in use on the sample (H3)
Sent sample  : 500 messages over 365 days -- reply history available (H4)
```

The last four lines are profile findings and each carries the heuristic that
produced it, so a reader can trace a proposal back to its evidence. A degraded
profile prints its NOTE lines here (§1.2) and the affected questions revert to
their cold form.

The owner domain is **detected, not asked**, from the authenticated account
returned by `provider.authenticate()` (`cli.py:91` already receives it). It
writes the existing `VoiceRouting.owner_domain` (`config.py:238`). A technician
running against a mailbox at a client site should never be typing the client's
domain from memory; it is one string away in the credential we just obtained.
`--owner-domain` overrides it for the case where the authenticated account is
an administrative mailbox on a different domain from the users it serves.

> **UNVERIFIED — message count.** Whether "8,412 in Inbox" is cheaply available
> is not established. `list_inbox` (`providers/base.py:217`) returns messages,
> not a count, and the Graph adapter caps at `MAX_PAGE_SIZE = 200`
> (`providers/outlook.py:77`). Graph exposes `totalItemCount` on a mail folder
> resource, which would settle it, but this spec does not assert that it is
> populated or accurate for Inbox. **What would settle it:** one live
> `GET /me/mailFolders/inbox` against a real tenant, reading
> `totalItemCount`. If unavailable, print `Messages: (not counted)` and let §4's
> dry-run report the size of the sample it actually read. Do not print an
> estimate.

---

### Q1 — Folder scope

**The folder tree is enumerated live and displayed before the prompt.** Nobody
can answer this from memory, and a technician certainly cannot.

**Pre-answered by the profiler (H2).** When the profile ran cleanly, the prompt
is not "which folders?" — it is "these look like matter files; exclude them?",
with the evidence that produced the guess:

```
This mailbox has 34 top-level folders, 61 including subfolders.

Six of them look like matter files or archives. Proposed: EXCLUDE.

                        messages   last activity   why
   Clients                  4,182   2 days ago     14 subfolders, matter-number shape
   Matters                  2,940   1 day ago      12 subfolders, matter-number shape
   Archive                  9,610   8 months ago   name matches 'archive'
   2023                     1,205   14 months ago  name matches a year
   2022                     1,890   26 months ago  name matches a year
   Closed Files               770   4 months ago   name matches 'closed'

These are proposals from folder names, counts, and dates -- not from reading
any mail. They can be wrong.

  [1] Inbox only                                    <- proposed, and the safe answer
  [2] Everything except those 6 folders
  [3] Everything except a list I name
  [4] Only the folders I name
  [5] Everything (including the 6 above)

  [k] Keep one of the six after all -- I'll say which

Choice [1]:
```

**Cold form**, used when the profile is absent or degraded (§1.2) — the same
listing rules, no proposals:

```
This mailbox has 34 top-level folders:

   1  Inbox                      (2 subfolders)
   2  Archive
   3  Clients                    (18 subfolders)
   4  Matters                    (12 subfolders)
   5  Action
   6  Waiting
   7  Admin
   8  Billing
   9  Conflicts
  10  Deleted Items
  ... 24 more.  Type 'more' to page, 'all' to print the full tree.

Which folders should the connector read and file into?

  [1] Inbox only                       <- default, and the safe answer
  [2] Everything except the folders I name
  [3] Only the folders I name
  [4] Everything

Choice [1]:
```

- **Answer type:** single choice; list-taking choices then prompt for a
  comma-separated list of numbers or names (`2,3,10` or `Archive,Clients`).
- **Default:** `1` — Inbox only.
- **Writes:** `folder_scope` (NEW) — an object
  `{"mode": "inbox_only" | "all_except" | "only_listed" | "all",
    "folders": ["..."]}`.
- **If skipped:** Inbox only. Rationale: it is the narrowest scope that still
  makes the product do its job, and it is the scope where a wrong answer is
  cheapest to correct. Widening later via a re-run (§5) is safe; narrowing after
  the connector has already filed mail out of `Clients/Vance-Matter-3` is a
  cleanup job for a human.

**Presenting a large tree without flooding the terminal.** The rules:

1. Show **top-level folders only**, with a subfolder count in parentheses.
2. Cap the initial listing at **10 entries**, then `... N more`.
3. `more` pages 10 at a time. `all` prints the full recursive tree and is
   explicitly the user's choice to flood their own terminal.
4. A tree deeper than 2 levels prints as `Clients/ (18 subfolders, 3 levels)`
   and is never expanded inline; scope selection operates on the top-level
   folder and includes its descendants.
5. If the mailbox has more than **200 top-level folders**, skip the listing
   entirely and print: `This mailbox has 340 top-level folders — too many to
   review here. Inbox-only scope is strongly recommended; use --answers with an
   explicit folder list if you need more.` Default stays `1`.

**Selecting a parent folder includes its subfolders.** Stated in the prompt
when the user picks 2 or 3: `Selecting 'Clients' includes its 18 subfolders.`
This is the answer a human expects, and the alternative — requiring 18
selections — is the kind of interview nobody finishes.

> **UNVERIFIED — folder enumeration mechanics.** The Graph adapter today calls
> `GET /me/mailFolders` with `$top=200` (`providers/outlook.py:872`) and reads
> `value` **flat**: it does not request `childFolders`, does not recurse, and
> does not follow `@odata.nextLink`. So the subfolder counts and the recursive
> total this prompt displays are **not obtainable from the current code path**.
> A separate lane is researching Graph folder enumeration and paging right now.
> This spec states the required *presentation* and does not guess the
> *mechanism*. **What would settle it:** that lane's finding on (a) whether
> `$expand=childFolders` returns counts in one call or requires N calls, (b)
> whether `childFolderCount` is populated on the folder resource, and (c) the
> real page size and `@odata.nextLink` behaviour on a mailbox with >200
> folders. Until it lands, treat the "(18 subfolders)" annotation as
> conditional: if the count is not available in one round trip, print the
> folder name alone rather than issuing 34 extra requests during an interview.
> Gmail's label model is flat with `/`-delimited names and will need its own
> presentation rule; not specified here.

---

### Q2 — System folders

Asked **separately** from Q1, as one question covering three folders, because
their default is different from every other folder and lumping them into Q1
would let a `4` (Everything) silently include Deleted Items.

Prompt text:

```
Junk Email, Deleted Items, and Archive are excluded by default.
Including them means the connector reads mail the owner already dismissed,
and can move messages out of them.

Include any of these?  (comma-separated, or Enter for none)
  [j] Junk Email
  [d] Deleted Items
  [a] Archive

Include []:
```

- **Answer type:** zero or more of `j`, `d`, `a`.
- **Default:** none included.
- **Writes:** `system_folders` (NEW) —
  `{"junk": false, "deleted": false, "archive": false}`.
- **If skipped:** all three excluded.
- **Applies regardless of Q1.** Choosing `4` (Everything) in Q1 does **not**
  include system folders; this question is the only way in. Stated in Q1's
  choice 4 text: `[4] Everything (system folders still excluded — see next
  question)`.

Rationale for excluding Archive by default, which is the debatable one:
Archive is where an owner puts mail they have finished with. Reading it is
defensible; *filing out of it* is the connector relocating things the owner
already put away, and the connector's only mutation for a message in scope is
`file_message`. Until the interview can offer read-without-file per folder
(not proposed here), excluded is the honest default.

---

### Q3 — Output instrument: categories vs folder moves

**Pre-answered by the profiler (H5 shape, H3 categories).** The recommendation
line and its evidence come from the profile; the choice list does not change:

```
This mailbox has 8,412 messages in the Inbox and 61% are unread, at about
47 a day. Recommended: folder moves -- the Inbox is where the problem is.

You already have 6 Outlook categories, 3 of them in use:
    Client (143 of 500 sampled)   Matter (88)   Intake (12)
    Q3-Push (0)   Old-Import (0)   Temp (0)
If you choose categories, the connector extends that vocabulary rather
than replacing it.
```

On an already-tidy mailbox (H5) the same line reads: `This mailbox has 118
messages in the Inbox and most mail is already filed. Recommended: categories
-- there is little to sweep, and this client already has a system.`

Prompt text, with the tradeoff stated in the prompt itself rather than in a
manual nobody opens:

```
How should the connector mark what it classified?

  [1] Categories — tag the message where it sits. Nothing moves, the
      Inbox stays as it is, and the owner filters or sorts by the tag.
      Reversible by removing a tag. Recommended for the first 30 days.

  [2] Folder moves — move the message into a folder. The Inbox empties,
      which is the point of the product, but mail is no longer where the
      owner last saw it. Reversible by moving it back.

You can switch later by re-running setup; existing filings are not undone.

Choice [1]:
```

- **Answer type:** single choice `1-2`.
- **Default:** `1` — categories.
- **Writes:** `output_instrument` (NEW) — `"categories" | "folders"`.
- **If skipped:** categories.

**Why categories is the default despite folder moves being the headline
feature.** The product's promise is inbox zero, which categories alone do not
deliver. But the first install on a real attorney's mailbox is the moment the
system is least trusted and most likely to be misconfigured, and a wrong
category is a tag to delete while a wrong move is mail the owner cannot find.
The prompt says "recommended for the first 30 days" and §5's re-run path makes
the switch cheap, so the default costs a re-run rather than a lost message.

> **UNVERIFIED — category behaviour on Graph, and the whole instrument on
> Gmail.** This is the largest unverified dependency in this spec and a lane is
> researching it now. What is established from the code: the Graph adapter
> **reads** `categories` on a message (`providers/outlook.py:98`, `:109`), and
> those values feed the deterministic sales-voice rule (`:104`). Nothing in the
> package **writes** a category — the only filing path is
> `POST /me/messages/{id}/move` (`providers/outlook.py:31`,
> `outlook.py:file_message`). So choice `[1]` is **not implementable against
> the current adapter** and requires a new provider capability.
>
> That has a security consequence this spec must not gloss: `providers/base.py`
> declares the capability surface closed at seven methods and states that adding
> one is "a security-posture change, not a feature", enforced by
> `tests/test_security_surface.py`. **An eighth capability (`set_category` or a
> mode on `file_message`) must go through that review, not through this
> interview.** Whether the master-category list must be pre-created on the
> mailbox, whether an arbitrary category string can be applied to a message,
> whether it requires a scope beyond `Mail.ReadWrite`, and what Gmail's
> equivalent is (labels are already the Gmail filing primitive, so the
> distinction may collapse there) are all open. **What would settle it:** the
> categories lane's live-tenant finding on PATCH-ing `categories` on a message,
> plus the master-list question, plus a statement of what Gmail's instrument
> split even means given labels. Until that lands, `setup` should refuse choice
> `[1]` with `not yet implemented on this provider` rather than write a config
> field nothing honours — a config that claims a posture the code does not
> implement is the failure mode `config.py:612` exists to prevent.

---

### Q4 — Taxonomy: create ours, or map onto theirs

The interview **looks at the mailbox before asking**, and the prompt differs
based on what the profiler found (H1 for folders, H3 for categories).

**Q4a — when likely-existing taxonomy folders are detected:**

```
This mailbox already has folders that look like a filing system:

                messages   last activity
    Action           214   today          matched 'action'
    Waiting           38   yesterday      matched 'waiting'
    Reference      1,102   3 days ago     matched 'reference'
    Billing          640   today          matched 'billing'
    Admin            310   today          (no keyword match -- listed for context)

The connector normally creates its own nine folders. It can instead file
into the ones already here, so the owner keeps the system they use.

  [1] Map onto the folders already in this mailbox   <- default
  [2] Create the nine default folders alongside them
  [3] Create the nine default folders and ignore the existing ones

Choice [1]:
```

**Q4b — when nothing resembling a taxonomy is found:**

```
No existing filing structure detected. The connector will create its
nine default folders:

  Business-Action, Business-Waiting, Business-Reference,
  Personal-Action, Personal-Reference, Receipts, Newsletters,
  Not-Interested, Review

Create them?  [Y/n]:
```

- **Answer type:** Q4a single choice `1-3`; Q4b yes/no.
- **Default:** Q4a `1` (map); Q4b yes.
- **Writes:** `taxonomy.json` via the existing `write_taxonomy`
  (`taxonomy.py:265`), with the mapped names as `folders` and the **default
  descriptions carried over** to whichever concept each mapped folder serves.
- **If skipped:** map when a structure was detected, create the nine defaults
  when none was.

**How the mapping is elicited.** For each of the nine default concepts, in
order, offer the detected folders plus "create it":

```
  Business-Action   -- work mail the owner must personally answer or decide.
                       Detected candidate: Action
    [1] Action        [2] Waiting      [3] Reference
    [4] Admin         [5] Billing
    [c] Create 'Business-Action'
    [s] Skip -- nothing is ever filed as Business-Action
  Choice [1]:
```

Three rules make this survivable:

1. **A candidate is suggested, never auto-applied.** Suggestion is a
   deterministic string match: exact, case-insensitive, and
   suffix/prefix-insensitive on the concept's distinguishing word
   (`Action` matches `Business-Action` and `Personal-Action`; the interview
   offers it for both and the technician resolves the collision). No model
   participates.
2. **`s` (skip) is a legal answer and it is not a failure.** A firm mailbox
   with no personal mail maps nothing to `Personal-Action`, and the resulting
   taxonomy simply does not contain it — the closed vocabulary shrinks, which
   `taxonomy.py:123` already permits (`MAX_FOLDERS` is a ceiling, not a floor).
3. **`Review` cannot be skipped.** It is the only concept the interview
   refuses to drop, because it is what lets the classifier decline to guess
   (`taxonomy.py:89`). If the user tries: `Review is required. It is where the
   classifier puts anything it is not sure about; without it, every uncertain
   message is forced into a confident wrong answer.` If no existing folder
   suits it, it is created.

**The stated case is honoured:** a firm that already has `Action/` and
`Waiting/` keeps them, and the connector files into `Action` and `Waiting` with
the `Business-Action` and `Business-Waiting` descriptions attached, so the
classifier's instructions stay precise while the folder names stay the firm's.

> **Note on `folder_prefix`.** `config.py:130` defaults it to empty and
> `config.py:521` documents why (an owner seeing a sidebar of folders nobody
> named). The interview should **not** ask about the prefix — see §6. Mapping
> onto existing folders makes the prefix question moot for the mapped concepts
> and it is already correctly defaulted for the created ones.

---

### Q5 — Business/personal split

The question that most needs technician-answerable phrasing, because "do you
get personal mail here?" is a question about someone else's life.

Prompt text — **structural, about the mailbox, not about habits:**

```
Is this a personal mailbox or a shared/role mailbox?

  [1] One person's mailbox  (jvance@vancecarter.example)
  [2] Shared or role mailbox  (intake@, billing@, info@, a departed
      employee's mailbox, a distribution address)

Choice [1]:
```

Then, **only if `[1]`:**

```
Does this mailbox carry personal mail as well as work mail?
A technician who does not know can answer 'unknown'.

  [y] Yes -- keep Personal-Action and Personal-Reference
  [n] No  -- work mail only; personal folders are not created
  [u] Unknown  <- default. Keeps the personal folders; nothing is lost
      if they stay empty.

Choice [u]:
```

- **Answer type:** `1-2`, then `y`/`n`/`u`.
- **Default:** `[1]` then `[u]`.
- **Writes:** `mailbox_kind` (NEW) — `"personal" | "shared"`; and
  `personal_split` (NEW) — `"yes" | "no" | "unknown"`.
- **If skipped:** personal mailbox, unknown split — which keeps the personal
  folders.
- **Not asked at all when `[2]`:** a shared mailbox writes
  `personal_split: "no"` structurally and the personal folders are dropped
  from the taxonomy. This is the §2.3 third case: the question is meaningless,
  so it is not asked rather than defaulted.

**Why `unknown` is a first-class answer and the default.** A technician who
guesses `no` on an owner's mailbox causes personal mail to be classified as
business and filed where a paralegal may be looking. A technician who answers
`unknown` gets both folder sets, and the cost is two folders that may stay
empty. Asymmetric cost, so the default is the harmless side. An interview that
forces a binary where the honest answer is "I don't know" manufactures a
decision, and this codebase already takes the opposite position everywhere —
`Review` at `taxonomy.py:89`, external-voice fallback at `config.py:66`.

---

### Q6 — Who reads the Review folder

**The most important question in the interview**, and the one most likely to be
cut for being awkward.

`Review` is the escape hatch that lets the classifier never guess.
`taxonomy.py:89` states its purpose plainly: choosing it is a correct answer,
never a failure. But an escape hatch nobody empties is not an escape hatch — it
is a second inbox, growing quietly, and after six weeks it is worse than the
inbox it was supposed to protect. **The classifier's licence to say "I don't
know" is only honest if a named human answers.**

Prompt text:

```
Messages the classifier is not sure about go to 'Review'. Nothing is
guessed and nothing is hidden -- but Review only works if somebody
actually opens it.

Who checks Review, and how often?

  [1] The mailbox owner, daily
  [2] The mailbox owner, weekly
  [3] Someone else -- I'll name them
  [4] Nobody is assigned yet          <- default

Choice [4]:
```

If `[3]`: `Name or email of the person who checks Review:` (free text, stored
verbatim, no validation beyond non-empty and a length cap).

- **Answer type:** single choice `1-4`, plus optional free text.
- **Default:** `4` — nobody assigned.
- **Writes:** `review_owner` (NEW) —
  `{"assignee": "owner" | "other" | "unassigned", "name": "...",
    "cadence": "daily" | "weekly" | "none"}`.
- **If skipped:** unassigned.

**What we do with the answer — this is the part that makes the question worth
asking.** An answer nobody acts on is decoration. Three consequences:

1. **It is printed in the dry-run summary and the completion summary**, by
   name: `Review is checked by: Dana Whitfield, weekly.` or, when unassigned,
   the warning below. The technician leaves the install having read the name
   out loud, which is the cheapest possible forcing function.

2. **`[4]` produces a standing warning, not a silent default.** At the end of
   `setup`, and again at the top of every `digest` output (`cli.py:192`):

   ```
   WARNING: nobody is assigned to Review. The classifier files anything it
   is unsure about there and will keep doing so. Unread Review messages: 12.
   Assign someone with 'inbox-zero-connector setup --answers ...' or accept
   that this folder is a queue nobody owns.
   ```

   `digest` is the right carrier because it is the command an MSP already runs
   to check on a deployment, and the warning is a count, not a nag — it stops
   being alarming when the count is zero.

3. **The cadence sets a staleness threshold the digest reports against.**
   `daily` -> flag Review messages older than 2 days; `weekly` -> older than 9
   days; `none` -> report the count with no threshold. This is deterministic
   arithmetic over the ledger and message timestamps, not a judgement, so it
   belongs in a script (`AGENTS.md` determinism law) and needs no model.

Deliberately **not** proposed: emailing the assignee. The connector cannot
send (`providers/base.py`, the absence is the product) and this spec does not
propose changing that.

---

### Q7 — Reply drafts posture

Included because it is the one existing security-posture field an interview
must not silently default, and because `init-config`'s current flags are
exactly the wrong ergonomics for it: `--enable-google-drafts` is a flag whose
help text (`cli.py:230`) carries a scope-layer-to-code-layer explanation that
nobody reads before typing a flag.

Prompt text (Microsoft):

```
Should the connector write reply drafts?

Drafts are written into the Drafts folder and can never be sent -- the
connector has no send capability at all. On Microsoft this costs nothing:
Mail.ReadWrite already covers reply drafts, and the no-send guarantee
stays enforced by the scopes Microsoft granted.

  [y] Yes
  [n] No   <- default

Choice [n]:
```

Prompt text (Google) — different, because the trade is real:

```
Should the connector write reply drafts?

On Gmail this costs something. Reply drafts require gmail.compose, and
Gmail's send methods also accept that scope. The connector still cannot
send -- no send code path exists and two build scanners assert it -- but
the guarantee moves from "Google's servers refuse it" to "our code has no
path to it". Your client may care about that distinction.

  [y] Yes, and I accept the code-layer posture
  [n] No, keep the scope-layer guarantee   <- default

Choice [n]:
```

- **Answer type:** `y`/`n`.
- **Default:** `n`, both providers, matching `default_config()`
  (`config.py:382`).
- **Writes:** existing `drafts_enabled` (`config.py:313`).
- **If skipped:** off.
- **Re-auth consequence stated inline** when the answer flips Google from off
  to on: `This changes the scopes requested. Run 'inbox-zero-connector auth
  google' again after setup completes.`

---

### Q8 — Sales-voice labels

Prompt text:

```
The voice routing sends replies on labelled threads to a 'sales' register
instead of the general external one. Which labels or categories on this
mailbox mean 'sales'?  (comma-separated, or Enter for none)

Labels seen on recent threads: Client, Matter, Intake, Referral, Vendor

Labels []:
```

- **Answer type:** comma-separated free text.
- **Default:** none.
- **Writes:** existing `VoiceRouting.sales_labels` (`config.py:241`).
- **If skipped:** empty tuple, which is what `init-config` produces today
  (`cli.py:112`). Every thread then routes internal-or-external per
  `config.py:861`, which is a complete and safe behaviour.

The observed-labels line is detection, not a suggestion: it lists what exists
so the technician recognises the vocabulary, and suggests nothing. On a law
firm mailbox the honest answer is usually "none" — firms do not have a sales
register — and the prompt must not push toward a non-empty answer.

---

### Q9 — Never-answered senders

Asked **only when the profile produced reply history (H4)**. Skipped entirely
when the sent read was unavailable or degraded — there is no cold form, because
without reply history there is nothing to ask about.

```
These senders write often and the owner has never once replied to them,
across 500 sent messages over the last year:

                                      messages   ever replied
    news@legaltechweekly.example            84   no
    updates@barassociation.example          61   no
    marketing@casewareplus.example          47   no
    noreply@docketalerts.example            39   no
    events@lipracticegroup.example          22   no

Proposed: file mail from these senders as Not-Interested by default.
A never-answered sender is usually a list, not a person -- but 'never
replied' can also mean the owner answers by phone.

  [1] Yes, file all five as Not-Interested   <- proposed
  [2] Let me pick which ones
  [3] No, classify them like everything else

Choice [1]:
```

- **Answer type:** single choice, with `2` prompting for a subset.
- **Default:** `1` when the profile ran; question absent otherwise.
- **Writes:** `never_replied_senders` (NEW) — a list of addresses feeding a
  deterministic rule row in `classify.py`'s `RULES` table.
- **If skipped:** empty list. Those senders classify normally, which is the
  behaviour today.

**Why this earns a question rather than being applied silently.** H4 is the
strongest signal available and it is injection-proof — an address either is or
is not in the sent set, and no hostile email can change that (§1.3 H4). That
makes it a *rule*, and `classify.py` is explicit that a rule carries authority a
heuristic must not borrow: a heuristic wearing a rule's clothing is "the worst
of both layers". Reply history is a genuine fact about the mailbox, but "never
replied means unwanted" is an inference on top of it, and the human confirms
that inference once rather than the classifier assuming it forever.

Deliberately **not** proposed: the inverse rule. "Owner has replied, therefore
Action" would push mail into the folder that demands attention on the strength
of an old correspondence, and a wrong Action is a wrong the owner has to
personally clear. The asymmetry that governs every default in this spec governs
this one: the signal is used only in the narrowing direction.

---

## 4. The dry-run requirement

**`setup` writes nothing — not a config file, not a taxonomy file, and not a
single folder in the mailbox — until it has shown what would happen against
real current mail and received an explicit confirmation.**

This is the same principle the connector already applies to mail: every
mutation is receipted (`ledger.py`), reversible, and never taken on the AI's
say-so. The install is a mutation of the owner's mailbox structure, and it is
the one mutation with no ledger row behind it. The dry-run is its receipt,
shown before the fact instead of after.

### 4.1 What the dry-run reads and what it must not do

- Reads: the folder tree (already enumerated in Q0), and a bounded sample of
  in-scope messages — **50 by default, `--sample N` up to 200** — via the
  existing `list_inbox`.
- Runs: the deterministic rule pass from `classify.py`, and the model pass on
  whatever the rules do not settle, exactly as a real sweep would.
- **Does not:** create a folder, move a message, apply a category, set a flag,
  write a draft, write a ledger row, or write any file in the config directory.

The last point needs a code-shaped answer, not a promise. The dry-run should
run against a **provider wrapper whose mutating methods raise**, so a dry-run
that accidentally reaches `file_message` crashes loudly instead of silently
filing. A guard that can be forgotten is worth less than a method that is not
there — the same argument `providers/base.py` makes about `send`.

### 4.2 Exact output shape

```
================================================================================
DRY RUN -- nothing has been changed. No folders created, no mail moved.
================================================================================

Mailbox            jvance@vancecarter.example  (Microsoft 365)
Scope              Inbox only
System folders     Junk: excluded   Deleted: excluded   Archive: excluded
Instrument         Categories (nothing will move)
Messages in scope  8,412
Sample classified  50 most recent

FOLDERS THAT WOULD BE CREATED (3)
  Business-Reference          new
  Not-Interested              new
  Review                      new

FOLDERS THAT WOULD BE REUSED (4)
  Action          -> Business-Action        (already exists, 214 messages)
  Waiting         -> Business-Waiting       (already exists, 38 messages)
  Reference       -> Personal-Reference     (already exists, 1,102 messages)
  Billing         -> Receipts               (already exists, 640 messages)

FOLDERS NOT MAPPED (2)
  Personal-Action             skipped -- nothing will be filed here
  Newsletters                 skipped -- nothing will be filed here

SAMPLE CLASSIFICATION (50 messages)
  Business-Action        14   ####################
  Business-Reference     11   ###############
  Receipts                8   ###########
  Business-Waiting        6   ########
  Not-Interested          6   ########
  Review                  5   #######
                          --
  by rule                 22  (deterministic, no model)
  by model                28

  Five examples, one per folder:
    Business-Action     "Re: Vance/Carter -- signature needed by Friday"
    Business-Waiting    "Re: records request -- awaiting county response"
    Receipts            "Your Westlaw invoice for August"
    Not-Interested      "Legal tech webinar: 5 AI trends for 2027"
    Review              "Fwd: (no subject)"

REVIEW FOLDER
  Checked by  Dana Whitfield, weekly
  Estimated volume at this rate: about 10% of incoming mail.

WOULD ALSO WRITE
  ~/.config/inbox-zero-connector/connector-config.json   (new)
  ~/.config/inbox-zero-connector/taxonomy.json           (new)

Reply drafts       Microsoft: OFF   (no drafts will be written)
Re-auth needed     no

================================================================================
Nothing above has happened yet.

Apply this configuration? Type 'apply' to proceed, anything else to abort.
> 
================================================================================
```

### 4.3 Rules governing the output

1. **The banner is the first and last thing on screen.** A technician who
   scrolls to the bottom and a technician who glances at the top both see
   "nothing has been changed".
2. **Confirmation is typing `apply`, not `y`.** A single keystroke is a
   keystroke someone makes while reading something else. This is the only
   destructive-adjacent moment in the install.
3. **Subject lines are truncated to 60 characters and are the only mail
   content that appears.** No bodies, no senders, no addresses. The reason is
   the same one `ledger.py:20` gives for the ledger storing no content: a
   dry-run pasted into a ticket should not be a mail leak. A technician's
   terminal scrollback on an MSP support desk is not a confidential channel.
4. **Percentages are computed from the sample and labelled as such.**
   "about 10% of incoming mail" carries "at this rate"; it is never stated as
   a property of the mailbox.
5. **A folder listed under "would be created" that already exists is a bug**,
   and the dry-run should say `already exists` rather than `new`. This is the
   check that makes §5's re-run safe, so it is worth stating as a requirement
   of the dry-run rather than only of the writer.
6. **Aborting prints nothing further and exits 0.** Aborting is a correct
   outcome, not an error, and an exit code of 1 would make a technician think
   something broke.
7. **`--dry-run-only`** performs everything above and exits 0 without
   prompting. This is the reviewable artifact an MSP shows a client before
   touching the mailbox.

> **UNVERIFIED — per-folder message counts.** "(already exists, 214 messages)"
> depends on the same `totalItemCount` question flagged in Q0. If unavailable,
> print the folder name without a count rather than issuing a count query per
> folder during an interview.

---

## 5. Re-running setup

An existing install must be reconfigurable without losing history or
duplicating folders. Re-running is expected, not exceptional: Q3's default
explicitly tells the user to switch instruments later, and a firm reorganising
its folders is a normal event.

### 5.1 Preserved — never touched by a re-run

| Artifact | Why |
| --- | --- |
| `ledger.jsonl` | Append-only by construction (`ledger.py:20`). A re-run appends nothing and truncates nothing. An audit trail a reconfiguration can shorten is not an audit trail. |
| Stored OAuth credential | `setup` never touches the keychain. Only `auth` and `logout` do. |
| Every message already filed | A re-run does not re-file, un-file, or move anything that was previously filed. The mailbox's current state is the owner's state. |
| Folders previously created by the connector | Not renamed, not deleted, not merged. If a re-run maps `Business-Action` onto `Action` where it previously created `Business-Action`, the old folder **stays with its mail in it**, and the dry-run says so explicitly (see 4.3). |
| Log files | Rotation is `logging_config`'s business. |

### 5.2 Overwritten — replaced wholesale on confirmation

| Artifact | Behaviour |
| --- | --- |
| `connector-config.json` | Rewritten in full via `write_config`. This already requires `--force` on `init-config` (`cli.py:105`); `setup` supersedes that gate with its own confirmation, which is stronger because it shows a diff first. |
| `taxonomy.json` | Rewritten in full via `write_taxonomy`. |
| All NEW fields in §3 | Rewritten. |

There is no partial merge. A half-updated config is a config nobody can reason
about, and the dry-run makes a full rewrite reviewable, which is what a merge
was trying to buy.

### 5.3 The re-run dry-run shows a diff, not just a target

Detected by the presence of `connector-config.json`. The dry-run gains a
section, placed above "folders that would be created":

```
CHANGES FROM THE CURRENT CONFIGURATION
  Instrument       Categories        ->  Folder moves      CHANGED
  Scope            Inbox only        ->  Inbox only        same
  Review owner     (unassigned)      ->  Dana Whitfield    CHANGED
  Business-Action  Business-Action   ->  Action            REMAPPED
  Drafts (MS)      off               ->  off               same

  REMAPPING NOTE
    'Business-Action' was created by a previous setup and holds 214
    messages. Those messages are NOT moved. New mail will be filed into
    'Action' instead. Both folders will exist until somebody merges them
    by hand.
```

The remapping note is the honest statement of a real consequence, and burying
it would be exactly the kind of confident silence that costs trust later. The
connector will not merge the folders itself: that is a bulk move of 214
messages, which is a mutation nobody asked for at install time.

### 5.4 Folder creation is idempotent

Creation goes through the existing resolve-or-create path
(`providers/outlook.py:_folder_id`), which matches on `display_name` before
creating. A second `setup` with the same answers creates zero folders. The
dry-run in 3.2 asserts this by showing them all as `already exists`, which is
what makes "run it again if you're unsure" honest advice rather than a risk.

### 5.5 Never a fresh-install path

`setup` has no `--reset`, no `--clean`, and no flag that removes folders,
messages, or ledger rows. Undoing an install is the owner's move in their own
mail client, the same way undoing a filing is. This matches the connector's
existing posture — there is no delete capability anywhere in the package
(`providers/base.py`) — and an installer that can delete what the runtime
cannot would be the widest capability in the product.

---

## 6. What the interview must NEVER ask

Naming these is worth as much as naming the good questions. **An interview
that asks what it could have detected is a defect** — it wastes the technician's
attention and, worse, it tells them the tool does not know the mailbox it is
about to reorganise.

### 6.1 Never ask for anything secret

| Never asked | Why | Where it actually comes from |
| --- | --- | --- |
| A password, of any kind | There is no path in this package that would accept one. Asking would train a technician to type a client credential into a CLI, which is a habit worth more to an attacker than any single password. | Nowhere. OAuth only. |
| An OAuth token, refresh token, or authorization code | Tokens live in the OS keychain and never touch disk (`cli.py:93`). | `auth`, via the browser flow. |
| The Google client secret / Microsoft client ID | Environment only, by design (`config.py:453`) — they are the buyer's own app-registration identifiers and keeping them out of files is what keeps them out of this repo. | `INBOX_ZERO_CREDENTIALS__*`. |
| An API key for any model | Not this component's business. | The host AI's own configuration. |
| Anything that would be echoed to a terminal and land in scrollback | An MSP tech's scrollback goes into tickets. | n/a |

### 6.2 Never ask what we can detect

| Never asked | Detected from |
| --- | --- |
| "What is your email address?" | The authenticated account (`cli.py:91`). |
| "What is your domain?" | Same, parsed. Only `--owner-domain` overrides, for the admin-mailbox case. |
| "Which provider?" | The credential that exists, or `INBOX_ZERO_PROVIDER` (`config.py:518`). |
| "What folders do you have?" | Enumerated live in Q0. Asking this is the specific defect this spec exists to prevent. |
| "Do you already have an Action folder?" | Q4's detection pass. |
| "How much mail do you get?" | Counted, or honestly reported as uncounted. |
| "What labels do you use?" | Profiler P2 + H3: the master category list plus in-use counts, displayed in Q3 and Q8. |
| "Which senders do you ignore?" | Profiler H4: reply history over the sent sample. The owner's own behaviour answers it better than their memory would. |
| "Are you drowning in mail, or on top of it?" | Profiler H5: Inbox count, unread ratio, filed-vs-unfiled. |
| "Do you already use Outlook categories?" | Profiler P2. |
| "Is this Exchange Online or on-prem?" | Implied by the token that worked. |
| "Where should config go?" | `platformdirs`, overridable by env (`config.py:402`). Asking for a path is asking a human to do a library's job. |

### 6.3 Never ask what the person at the keyboard cannot answer

These fail the technician test in §0. Each has a stated alternative, because
"do not ask" without an alternative is just a gap.

| Never asked | Why it fails | What happens instead |
| --- | --- | --- |
| "How formal is your writing?" / "What's your tone?" | A technician cannot answer for the owner, and the owner cannot introspect it accurately either. | The `voice-profile` skill derives it from the sent sample, with a "standard business" default when the sample is too thin (SPEC.md, Rico 2026-08-18). Never an interview question. |
| "Which clients are most important?" | Client names in an installer prompt are confidential information entering a terminal for no operational reason. | Not modelled. Importance comes from `is_important` and the rule pass. |
| "What are your matter numbers / case codes?" | Same confidentiality problem, plus it is a taxonomy the connector deliberately does not have — `MAX_FOLDERS` is 24 (`taxonomy.py:123`) and a per-matter vocabulary is unbounded. | Out of scope. The connector files by *what to do next*, not by *what it is about*. |
| "Do you want to keep newsletters?" | A preference about the owner's reading habits. | Default is Newsletters exists and is used. Owner adjusts by re-running. |
| "How aggressive should classification be?" | Not a dial that exists. There is no confidence threshold to tune; uncertainty goes to `Review` by construction (`classify.py`, `taxonomy.py:89`). Offering the dial would imply a control the code does not have. | Nothing. `Review` is the mechanism. |
| "Should the AI be allowed to send?" | It cannot, at any setting. There is no send code path (`providers/base.py`). A question implying otherwise is a false claim about the product's central guarantee. | Never asked. §3 Q7 asks about *drafts*, which is a real switch. |
| "What prefix do you want on folder names?" | `config.py:130` already settled this after live verification: a prefix serves us, not the owner, and empty is the right default. Re-opening it in an interview would reintroduce a decision that was made with evidence. | Empty. `INBOX_ZERO_FOLDER_PREFIX` remains for the operator who deliberately wants the old separation. |
| "How often should it sweep?" | The connector does not schedule anything. It is an MCP server; the host AI decides when to call it. | Not asked. Cadence in Q6 is about a *human* checking Review, not about the connector. |

### 6.4 Never ask a question whose answer nothing reads

The rule that catches future additions: **a question may be added to this
interview only if some code path reads the field it writes.** A field written by
`setup` and read by nothing is worse than an unasked question — it is a
configuration surface that implies a behaviour, and the first person to notice
the behaviour is absent will reasonably conclude the rest of the config is
decorative too.

Q6 is the test case for this rule and the reason it is stated. "Who reads
Review" is trivially answerable and trivially ignorable; §3 Q6 spends three
paragraphs on what consumes the answer specifically because a question without
a consumer should have been cut.

---

## 7. Worked transcript

A realistic end-to-end session. Law firm mailbox, Microsoft 365, MSP technician
at the keyboard, existing folder structure. Technician input is on `>` lines.

Every number, folder name, and subject line below is **invented for
illustration**. This transcript is a design artifact, not a captured run — no
`setup` command exists to capture.

```
$ inbox-zero-connector auth microsoft
Authenticating microsoft with exactly these scopes:
  https://graph.microsoft.com/Mail.ReadWrite
  https://graph.microsoft.com/User.Read
  offline_access

Drafts enabled for microsoft: False
no-send enforcement: scope-layer

Not requested, and not usable with the resulting token:
  https://graph.microsoft.com/Mail.Send   (would be required to send mail)

To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code H7K2-QM4P to authenticate.

Authenticated as jvance@vancecarter.example.
Credential stored in the OS keychain. No token was written to disk.

$ inbox-zero-connector setup

inbox-zero-connector 0.2.0 -- mailbox setup
Nothing is written or created until you confirm at the end.

Profiling the mailbox (read-only -- no folders created, no mail moved,
nothing sent). This takes about 20 seconds.

  folder tree ......... 12 top-level, 31 total          4 requests
  categories .......... 6 defined, 3 in use             1 request
  inbox sample ........ 500 of 8,412 messages           3 requests
  sent sample ......... 500 messages over 365 days      3 requests
                                                       11 requests total

Mailbox      : jvance@vancecarter.example  (Microsoft 365)
Owner domain : vancecarter.example         (detected from the authenticated account)
Folders      : 12 top-level, 31 total
Messages     : 8,412 in Inbox, 61% unread, about 47/day
Shape        : drowning -- folder moves recommended (H5)
Categories   : 6 defined, 3 in use on the sample (H3)
Sent sample  : 500 messages over 365 days -- reply history available (H4)

NOTE: this mailbox has 8,412 Inbox messages; the profile read the 500 most
recent. Sender rankings and the unread ratio describe that sample, not the
whole mailbox.

Everything below is a PROPOSAL. Nothing is applied until you confirm.

--------------------------------------------------------------------------------
1 of 9  --  Folder scope
--------------------------------------------------------------------------------

This mailbox has 12 top-level folders, 31 including subfolders.

Three of them look like matter files or archives. Proposed: EXCLUDE.

                        messages   last activity   why
   Clients                  4,182   2 days ago     14 subfolders, matter-number shape
   Archive                  9,610   8 months ago   name matches 'archive'
   Conflicts                  190   today          name matches 'closed'/'conflicts'
                                                   (recent activity -- less certain)

These are proposals from folder names, counts, and dates -- not from reading
any mail. They can be wrong.

  [1] Inbox only                                    <- proposed, and the safe answer
  [2] Everything except those 3 folders
  [3] Everything except a list I name
  [4] Only the folders I name
  [5] Everything (including the 3 above)

  [k] Keep one of the three after all -- I'll say which

Choice [1]:
> 1

Scope: Inbox only. (The three proposed exclusions are moot at this scope.)

--------------------------------------------------------------------------------
2 of 9  --  System folders
--------------------------------------------------------------------------------

Junk Email, Deleted Items, and Archive are excluded by default.
Including them means the connector reads mail the owner already dismissed,
and can move messages out of them.

Include any of these?  (comma-separated, or Enter for none)
  [j] Junk Email
  [d] Deleted Items
  [a] Archive

Include []:
> 

None included.

--------------------------------------------------------------------------------
3 of 9  --  Output instrument
--------------------------------------------------------------------------------

This mailbox has 8,412 messages in the Inbox and 61% are unread, at about
47 a day. Recommended: folder moves -- the Inbox is where the problem is.

You already have 6 Outlook categories, 3 of them in use:
    Client (143 of 500 sampled)   Matter (88)   Intake (12)
    Q3-Push (0)   Old-Import (0)   Temp (0)
If you choose categories, the connector extends that vocabulary rather
than replacing it.

How should the connector mark what it classified?

  [1] Categories -- tag the message where it sits. Nothing moves, the
      Inbox stays as it is, and the owner filters or sorts by the tag.
      Reversible by removing a tag. Recommended for the first 30 days.

  [2] Folder moves -- move the message into a folder. The Inbox empties,
      which is the point of the product, but mail is no longer where the
      owner last saw it. Reversible by moving it back.

You can switch later by re-running setup; existing filings are not undone.

Choice [1]:
> 2

Instrument: folder moves.

--------------------------------------------------------------------------------
4 of 9  --  Taxonomy
--------------------------------------------------------------------------------

This mailbox already has folders that look like a filing system:

                messages   last activity
    Action           214   today          matched 'action'
    Waiting           38   yesterday      matched 'waiting'
    Reference      1,102   3 days ago     matched 'reference'
    Billing          640   today          matched 'billing'
    Admin            310   today          (no keyword match -- listed for context)

The connector normally creates its own nine folders. It can instead file
into the ones already here, so the owner keeps the system they use.

  [1] Map onto the folders already in this mailbox   <- default
  [2] Create the nine default folders alongside them
  [3] Create the nine default folders and ignore the existing ones

Choice [1]:
> 1

Map each concept to a folder. 'c' creates it, 's' skips it entirely.

  Business-Action   -- work mail the owner must personally answer or decide.
                       Detected candidate: Action
    [1] Action     [2] Waiting     [3] Reference     [4] Billing     [5] Admin
    [c] Create 'Business-Action'      [s] Skip
  Choice [1]:
> 1

  Business-Waiting  -- work mail blocked on someone else's reply or signature.
                       Detected candidate: Waiting
    [1] Action     [2] Waiting     [3] Reference     [4] Billing     [5] Admin
    [c] Create 'Business-Waiting'     [s] Skip
  Choice [2]:
> 2

  Business-Reference -- work mail worth keeping with nothing to do.
                        Detected candidate: Reference
    [1] Action     [2] Waiting     [3] Reference     [4] Billing     [5] Admin
    [c] Create 'Business-Reference'   [s] Skip
  Choice [3]:
> 3

  Personal-Action   -- non-work mail needing a reply or an act from the owner.
                       No candidate detected.
    [1] Action     [2] Waiting     [3] Reference     [4] Billing     [5] Admin
    [c] Create 'Personal-Action'      [s] Skip
  Choice [c]:
> c

  Personal-Reference -- non-work mail worth keeping with nothing to do.
                        No candidate detected.
    [c] Create 'Personal-Reference'   [s] Skip
  Choice [c]:
> c

  Receipts          -- proof of a transaction: invoices, statements, payments.
                       Detected candidate: Billing
    [1] Action     [2] Waiting     [3] Reference     [4] Billing     [5] Admin
    [c] Create 'Receipts'             [s] Skip
  Choice [4]:
> 4

  Newsletters       -- bulk mail the owner deliberately subscribed to.
                       No candidate detected.
    [c] Create 'Newsletters'          [s] Skip
  Choice [c]:
> c

  Not-Interested    -- bulk or targeted mail the owner never asked for.
                       No candidate detected.
    [c] Create 'Not-Interested'       [s] Skip
  Choice [c]:
> c

  Review            -- the uncertainty bucket. Required; cannot be skipped.
                       No candidate detected.
    [c] Create 'Review'
  Choice [c]:
> c

Taxonomy: 9 concepts, 4 mapped to existing folders, 5 to be created.

--------------------------------------------------------------------------------
5 of 9  --  Mailbox kind
--------------------------------------------------------------------------------

Is this a personal mailbox or a shared/role mailbox?

  [1] One person's mailbox  (jvance@vancecarter.example)
  [2] Shared or role mailbox  (intake@, billing@, info@, a departed
      employee's mailbox, a distribution address)

Choice [1]:
> 1

Does this mailbox carry personal mail as well as work mail?
A technician who does not know can answer 'unknown'.

  [y] Yes -- keep Personal-Action and Personal-Reference
  [n] No  -- work mail only; personal folders are not created
  [u] Unknown  <- default. Keeps the personal folders; nothing is lost
      if they stay empty.

Choice [u]:
> u

Personal folders kept.

--------------------------------------------------------------------------------
6 of 9  --  Who reads Review
--------------------------------------------------------------------------------

Messages the classifier is not sure about go to 'Review'. Nothing is
guessed and nothing is hidden -- but Review only works if somebody
actually opens it.

Who checks Review, and how often?

  [1] The mailbox owner, daily
  [2] The mailbox owner, weekly
  [3] Someone else -- I'll name them
  [4] Nobody is assigned yet          <- default

Choice [4]:
> 3

Name or email of the person who checks Review:
> Dana Whitfield (office manager)

How often?
  [1] Daily    [2] Weekly
Choice [2]:
> 2

Review is checked by: Dana Whitfield (office manager), weekly.

--------------------------------------------------------------------------------
7 of 9  --  Reply drafts
--------------------------------------------------------------------------------

Should the connector write reply drafts?

Drafts are written into the Drafts folder and can never be sent -- the
connector has no send capability at all. On Microsoft this costs nothing:
Mail.ReadWrite already covers reply drafts, and the no-send guarantee
stays enforced by the scopes Microsoft granted.

  [y] Yes
  [n] No   <- default

Choice [n]:
> n

Drafts: off. The connector will classify and file only.

--------------------------------------------------------------------------------
8 of 9  --  Sales-voice labels
--------------------------------------------------------------------------------

The voice routing sends replies on labelled threads to a 'sales' register
instead of the general external one. Which labels or categories on this
mailbox mean 'sales'?  (comma-separated, or Enter for none)

Labels seen on recent threads: Client, Matter, Intake, Referral, Vendor

Labels []:
> 

None. Threads route internal or external only.

--------------------------------------------------------------------------------
9 of 9  --  Never-answered senders
--------------------------------------------------------------------------------

These senders write often and the owner has never once replied to them,
across 500 sent messages over the last year:

                                      messages   ever replied
    news@legaltechweekly.example            84   no
    updates@barassociation.example          61   no
    marketing@casewareplus.example          47   no
    noreply@docketalerts.example            39   no
    events@lipracticegroup.example          22   no

Proposed: file mail from these senders as Not-Interested by default.
A never-answered sender is usually a list, not a person -- but 'never
replied' can also mean the owner answers by phone.

  [1] Yes, file all five as Not-Interested   <- proposed
  [2] Let me pick which ones
  [3] No, classify them like everything else

Choice [1]:
> 2

Which ones?  (comma-separated numbers, or 'none')
   1 news@legaltechweekly.example
   2 updates@barassociation.example
   3 marketing@casewareplus.example
   4 noreply@docketalerts.example
   5 events@lipracticegroup.example
> 1,3,4

Three senders will file as Not-Interested.
(The bar association and the practice group stay in normal classification --
the technician recognised both as things the firm actually cares about.)

--------------------------------------------------------------------------------
Reading 50 recent messages to show you what would happen...
--------------------------------------------------------------------------------

================================================================================
DRY RUN -- nothing has been changed. No folders created, no mail moved.
================================================================================

Mailbox            jvance@vancecarter.example  (Microsoft 365)
Scope              Inbox only
System folders     Junk: excluded   Deleted: excluded   Archive: excluded
Instrument         Folder moves
Messages in scope  8,412
Sample classified  50 most recent

FOLDERS THAT WOULD BE CREATED (5)
  Personal-Action             new
  Personal-Reference          new
  Newsletters                 new
  Not-Interested              new
  Review                      new

FOLDERS THAT WOULD BE REUSED (4)
  Action          -> Business-Action        (already exists, 214 messages)
  Waiting         -> Business-Waiting       (already exists, 38 messages)
  Reference       -> Business-Reference     (already exists, 1,102 messages)
  Billing         -> Receipts               (already exists, 640 messages)

SAMPLE CLASSIFICATION (50 messages)
  Action                 14   ####################
  Reference              11   ###############
  Billing                 8   ###########
  Waiting                 6   ########
  Not-Interested          6   ########
  Review                  5   #######
                          --
  by rule                 22  (deterministic, no model)
  by model                28

  Five examples, one per folder:
    Action              "Re: Vance/Carter -- signature needed by Friday"
    Waiting             "Re: records request -- awaiting county response"
    Billing             "Your Westlaw invoice for August"
    Not-Interested      "Legal tech webinar: 5 AI trends for 2027"
    Review              "Fwd: (no subject)"

NEVER-ANSWERED SENDER RULE (3 senders)
  news@legaltechweekly.example, marketing@casewareplus.example,
  noreply@docketalerts.example  ->  Not-Interested
  6 of the 50 sampled messages matched this rule.

REVIEW FOLDER
  Checked by  Dana Whitfield (office manager), weekly
  Estimated volume at this rate: about 10% of incoming mail.

WOULD ALSO WRITE
  ~/.config/inbox-zero-connector/connector-config.json   (new)
  ~/.config/inbox-zero-connector/taxonomy.json           (new)

Reply drafts       Microsoft: OFF   (no drafts will be written)
Re-auth needed     no

================================================================================
Nothing above has happened yet.

Apply this configuration? Type 'apply' to proceed, anything else to abort.
> apply
================================================================================

Creating folders...
  Personal-Action        created
  Personal-Reference     created
  Newsletters            created
  Not-Interested         created
  Review                 created

Writing configuration...
  ~/.config/inbox-zero-connector/taxonomy.json           written
  ~/.config/inbox-zero-connector/connector-config.json   written

Setup complete.

  Mailbox      jvance@vancecarter.example
  Scope        Inbox only
  Instrument   Folder moves
  Drafts       off  (scope-layer no-send guarantee)
  Review       Dana Whitfield (office manager), weekly

  Save these answers for the rest of the rollout:
    inbox-zero-connector setup --print-answers > vancecarter.json
    inbox-zero-connector setup --answers vancecarter.json   (on each mailbox)

  Verify the security posture:  inbox-zero-connector show-scopes
  Check on it later:            inbox-zero-connector digest
```

### 7.1 The same rollout, mailboxes 2 through 40

```
$ inbox-zero-connector setup --print-answers > vancecarter.json

$ cat vancecarter.json
{
  "folder_scope":      { "mode": "inbox_only", "folders": [] },
  "system_folders":    { "junk": false, "deleted": false, "archive": false },
  "output_instrument": "folders",
  "taxonomy_mapping": {
    "Business-Action":    "Action",
    "Business-Waiting":   "Waiting",
    "Business-Reference": "Reference",
    "Receipts":           "Billing",
    "Personal-Action":    "__create__",
    "Personal-Reference": "__create__",
    "Newsletters":        "__create__",
    "Not-Interested":     "__create__",
    "Review":             "__create__"
  },
  "mailbox_kind":   "personal",
  "personal_split": "unknown",
  "review_owner":   { "assignee": "other",
                      "name": "Dana Whitfield (office manager)",
                      "cadence": "weekly" },
  "drafts_enabled": { "google": false, "microsoft": false },
  "sales_labels":   []
}

$ inbox-zero-connector auth microsoft        # per mailbox, unchanged
$ inbox-zero-connector setup --answers vancecarter.json --dry-run-only
    ... profile, full dry-run, exits 0, writes nothing ...

$ inbox-zero-connector setup --answers vancecarter.json --accept-proposals=safe
    ... profile, dry-run, then the 'apply' prompt ...
```

Note what this file does **not** contain: no mailbox address, no domain, no
provider, no credential, no path — and **no sender addresses, no counts, and no
subject lines** from the profile (§1.5). Those are detected or derived per
mailbox (§6.2), which is what makes one file serve 40 installs and keeps the
file itself from being a summary of somebody's mail. `never_replied_senders` is
deliberately absent: it is a per-mailbox fact, re-derived by each mailbox's own
profile and confirmed under `--accept-proposals` policy, never copied from one
client's mailbox to another's.

It contains one piece of a client's information — the Review owner's name — and
that is a deliberate cost, because §3 Q6 is worthless without a name attached.

**What the batch reports at the end**, per §1.6.1:

```
$ for mbox in $(cat mailboxes.txt); do
>   inbox-zero-connector setup --answers vancecarter.json --accept-proposals=safe
> done

38 of 40 mailboxes configured.

NEEDS A HUMAN (2)
  rgarcia@vancecarter.example   no 'Action' folder; the answer file maps
                                Business-Action -> Action. Refused rather
                                than creating a different structure here.
  intake@vancecarter.example    shared mailbox, and review_owner is
                                unassigned. Refused: nobody can be inferred
                                to staff the Review queue.
```

---

## 8. Open dependencies, collected

Every UNVERIFIED marker in this document, in one place, so the lanes that
settle them can be tracked against it.

| # | Dependency | Section | What would settle it |
| --- | --- | --- | --- |
| 1 | Graph message counts (`totalItemCount` on a mail folder, Inbox and named folders) | Q0, §4.3 | One live `GET /me/mailFolders/inbox` against a real tenant. If absent, print "(not counted)" — never estimate. |
| 2 | Graph folder enumeration: recursion, `childFolderCount`, `$expand=childFolders`, `@odata.nextLink`, real page size beyond the current flat `$top=200` (`providers/outlook.py:872`) | Q1 | The folder-enumeration lane's live finding. Presentation rules in Q1 hold regardless; the annotation detail is conditional. |
| 3 | Graph categories: whether an arbitrary category can be PATCHed onto a message, whether the master list must pre-exist, what scope it needs | Q3 | The categories lane's live finding. |
| 4 | Whether "categories vs folders" is even a meaningful split on Gmail, given labels are already the filing primitive | Q3 | Same lane, Gmail half. |
| 5 | Whether a category-writing capability is an eighth provider method, and therefore a security-surface change under `providers/base.py` and `tests/test_security_surface.py` | Q3 | A decision by whoever owns the provider boundary. **Not this spec's call.** |
| 6 | Gmail label-tree presentation (flat, `/`-delimited) for Q1 | Q1 | Design pass once the Gmail folder lane reports. |
| 7 | `GET /me/outlook/masterCategories` — whether the read is available under `Mail.ReadWrite` alone, or needs `MailboxSettings.Read`. **A profiler read that forces an extra scope is not worth having**; if it needs one, drop P2 and derive the vocabulary from categories observed on sampled messages only. | §1.1 P2, Q3 | The categories lane, or one live call with the current token. |
| 8 | Whether the profiler's narrower sent read (recipients and dates, no bodies) can reuse `read_sent_sample`/`SentSelection` (`providers/base.py:157`) or needs its own provider method — and if the latter, whether that is a security-surface change | §1.1 P4, §1.3 H4 | A decision by the provider-boundary owner. **Not this spec's call.** |
| 9 | Whether sender addresses need adding to `logging_config`'s redaction table before the profiler logs anything | §1.5 | Read the redaction table; add if absent. Cheap and settleable now. |
| 10 | The §1.2 cost claim ("10–15 requests, under 30 seconds") is PROPOSED arithmetic, not a measurement | §1.2 | One profiling run against a real mailbox, timed, with the request count logged. |
| 11 | Gmail equivalents for every P1–P4 read (labels for folders, no master-category concept, `SENT` label for sent mail) | §1.1 | Design pass once the Gmail lanes report. |

None of these blocks writing the interview's deterministic parts: Q2, Q4, Q5,
Q6, Q7, Q8, the answer-file mechanism, the confirmation gate, the
`--accept-proposals` policy, and the re-run rules depend on nothing unverified.
The profiler's H1, H2, H4, and H5 heuristics are arithmetic over data the
current adapter already reads, and are blocked only on #2 for folder counts.

---

## 9. Status recap

Every claim in this document is **PROPOSED**. No `setup` command exists, no
profiling pass exists, and no heuristic in §1.3 has ever run. The config fields
marked NEW do not exist in `config.py`. The dry-run has never run. The
transcript in §7 is invented, not captured, and every number in it —
message counts, request counts, timings, sender volumes, classification
distributions — is illustrative. What *is* verified is only the description of
the current code, cited by file and line throughout, read on 2026-08-18 at
connector version 0.2.0.

**The no-send property this spec declines to trade (§0.2) is the one claim here
that is not proposed**: it is verified in the code today — `providers/base.py`
implements no send method, `scopes.py` never requests `Mail.Send`, and
`tests/test_security_surface.py` fails the build if either changes.
