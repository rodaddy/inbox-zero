# Kit boundary analysis — what exists, what does not, what it costs

**Status: PROPOSED analysis. Nothing here is a decision.** Every claim about the
code is WRITTEN (read from disk this session) or RUNNING (a command executed
this session, exit code quoted). Every recommendation is PROPOSED and is rTech's
to accept or reject.

Analysed at `feat/inbox-zero-voice-corpus`, working tree as of 2026-08-20.
Baseline commit `807d721`. Paths are relative to
`kits/inbox-zero/` unless absolute.

---

## 0. A correction to the premise, before anything else

The lane brief states that `guardian` returns ZERO hits in the kit tree and that
the three-kit model exists only in the internal sprint notes.

**That is half right, and the half that is wrong changes the shape of the
problem.** RUNNING, this session:

```
rg -i -c "guardian" kits/inbox-zero/   -> exit=0, 5 files hit
rg -i -n "guardian" connector/src/          -> exit=1, ZERO hits
```

The hits are:

| File:line | What it says |
|---|---|
| `SPEC.md:18` | *"the kit is 'Inbox Zero'; the connector's product name is the 'rTech Email Guardian'"* |
| `SPEC.md:20` | *"Technical identifiers stay `inbox-zero-connector` until the Guardian is extracted for reuse."* |
| `SPEC.md:79` | `├── connector/   # the rTech Email Guardian MCP server` |
| `OPERATIONS-MANUAL.md:45` | *"Lane 1 — the rTech Email Guardian."* |
| `installer.md:12` | *"the rTech Email Guardian connector and its own README"* |
| `connector/README.md:1` | `# rTech Email Guardian` |
| `CAPABILITIES.md:949` | *"the Guardian tier becomes a latency…"* |

So the correction, and it is the most consequential thing in this document:

> **"Guardian" is already in use in the kit documentation as the product name of
> the WHOLE connector — every install of it, in every configuration. The
> planning README uses the same word for a RESTRICTED SECURITY TIER that is a
> strict subset of that connector.**

These two meanings are not compatible. Under `SPEC.md:18` the connector rTech
ships to anyone, drafting and voice included, IS the rTech Email Guardian.
Under the internal sprint plan the Guardian is the kit that
specifically does NOT have those things. `CAPABILITIES.md:949` says "Guardian
tier", which is the planning sense, inside a file whose sibling docs use the
product-name sense.

The cost of leaving this is not stylistic. A client reading `connector/README.md`
sees "rTech Email Guardian" on a component that composes reply drafts with an
LLM, and a client reading the MSP pitch is told Guardian means no model touches
their mail. Those two clients compare notes in the same procurement conversation.
The word cannot carry both loads.

**PROPOSED, rTech decides:** one of the two words moves. Either the connector's
product name changes and "Guardian" is reserved for the secure tier, or the
secure tier gets a different name. This is a naming decision with no code in it
and it blocks nothing, but it should be made before the word reaches a client
deck, because renaming a shipped product is expensive and renaming an unshipped
tier is free.

The brief's underlying point survives the correction intact: **there is no
Guardian TIER anywhere in the code.** `rg -i "guardian" connector/src/` exits 1.
The three-kit model has zero mechanical existence.

---

## Q1 — What gates capability today?

### The mechanism, verified

`connector/src/inbox_zero_connector/config.py`:

- **`config.py:367` `class Posture(StrEnum)`** — a closed set, `StrEnum` so a
  hand-typed member is a load error rather than a silent value
  (`config.py:369-371`).
- **`config.py:376`** — the boxed rule, verbatim: `POSTURE CAN ONLY NARROW. IT
  CANNOT WIDEN.`
- **`config.py:380`** — *"`FILE_READ_DRAFT` does not ENABLE drafts; it declines
  to forbid them."* The brief's characterisation is exact.
- **`config.py:417` `FILE_ONLY`**, **`config.py:422` `FILE_READ_DRAFT`** — the
  two members. `FILE_ONLY` is the default (`config.py:448`, `config.py:938`).
- **`config.py:572` `drafts_on(provider)`** — the SCOPE question: does this
  deployment request `gmail.compose`.
- **`config.py:576` `drafts_allowed(provider)`** — the AND of both, documented
  at `config.py:577` as *"BOTH gates, one answer… the only sanctioned way to ask
  the question."* Body at `config.py:597`:
  `return self.posture is Posture.FILE_READ_DRAFT and self.drafts_on(provider)`.
- **`config.py:1143`** — the announced row: posture is `file_read_draft` but the
  provider flag is false, so a warning fires naming the exact fix command.

Every line number in the brief was approximate (`~367`, `~576`, `~572`) and every
one of them is correct to the line. Nothing had drifted.

This is a genuinely well-built mechanism. The one-directional composition table
at `config.py:391-400`, the refusal to let posture reach a scope decision, and
the single sanctioned accessor are the right shape for what they do.

### The answer

**Capability is gated at RUNTIME, entirely.** `Posture` is a field in a config
file that the fully-featured installed package reads at load. Every capability
in the tree — drafting, voice, the LLM classifier — is present in the wheel
regardless of posture. There is no build-time selection anywhere:
`pyproject.toml` declares one dependency list with **no `[project.optional-dependencies]`
and no extras** (verified, `pyproject.toml:6-33`), one console script
(`pyproject.toml:35-36`), and one wheel target packaging the whole package
(`pyproject.toml:49-50`).

`openai>=1.50,<3` is an unconditional runtime dependency (`pyproject.toml:32`).
A Guardian install today ships the OpenAI client.

### One thing the brief did not ask about, and it is the sharpest finding here

`drafts_allowed` — the accessor documented as *the only sanctioned way to ask* —
has **exactly one production caller in the entire package**. RUNNING:

```
rg -n "drafts_allowed" src/ tests/
```

Production hits: `drafting.py:512` (inside `may_draft`), plus docstring mentions
at `drafting.py:35`, `drafting.py:495`, `config.py:458`. Everything else is
tests.

The MCP tool that composes drafts does not go through it. **`server.py:906`**,
inside `_validated_draft_request` (`server.py:883`), gates on
`config().drafts_on(name)` — the
SCOPE flag alone. Posture is never consulted on that path.

The consequence, stated plainly: **a deployment configured `posture = file_only`
with `drafts_enabled.google = true` will refuse to draft in the watcher and will
happily draft when the host AI calls the `create_draft` MCP tool.** That is
precisely the `file_only` + drafts-on row that `config.py:404` describes as
"silent by contrast: the operator asked for LESS than the scope permits" — and
on the MCP path they do not get less. They get the scope.

I did not execute this; it is WRITTEN-level, read from source. It is a
FINDING outside this lane's writable set and is reported, not fixed. It matters
to the kit question because posture is the only capability gate that exists, and
it has a hole in the surface a host AI actually drives.

Second, smaller: `watcher.py:621` calls `drafting.should_draft` (the mail
question) rather than `drafting.may_draft` (the deployment question). The
watcher is not exposed by this — its posture gate is structural: `watcher.py:618`
returns false when `draft_backend is None`, and **no production code path injects
a `draft_backend`.** RUNNING, `rg -n "draft_backend=" src/ tests/ scripts/`
returns exactly one hit, `tests/test_watcher_drafting.py:130`. The watcher's
drafting limb is dormant in the shipped CLI. That is absence by accident of
wiring rather than by design, and it will stop being true the moment the watcher
is wired for real.

---

## Q2 — Does the mechanism meet the plan's own standard?

The plan's standard, from the internal sprint notes:

> **Absence beats configuration.** A config field saying "do not store" is a
> promise; a build with no storage writer is a guarantee. `--guardian` installs
> only the deterministic pieces. This is why the no-send guarantee was granted
> without argument: there is no send method in the package. Not disabled. Absent.

### Where the codebase already achieves absence — the no-send guarantee holds

Verified, RUNNING:

```
rg -n "def send|\.send\(|sendMail|messages/send" src/inbox_zero_connector/
```

Every hit is a **docstring naming the thing that is not there**:
`providers/base.py:52`, `providers/outlook.py:7`, `:34`, `:1134`,
`scopes.py:84`, `scopes.py:236`. There is no `def send`. There is no call site.
The one non-doc hit is `outlook_models.py:103 sender_address`, a property
reading a From header.

The guarantee is reinforced at the scope layer: `scopes.py:126-132` documents
that `gmail.send` and `mail.google.com` *"are never requested under any
configuration"*, and `scopes.py:250` `GRAPH_SEND_WOULD_REQUIRE = ("Mail.Send",)`
names the permission the app registration omits.

**The no-send guarantee is real absence and survives inspection.** It is the
worked example the plan claims it is, and it is the standard everything else
should be measured against.

### Where Guardian today would be a config promise

Against that standard, a Guardian install produced by setting
`posture = file_only` today would be a **CONFIG PROMISE at every point**, because
the wheel is byte-identical to the standard kit. Concretely, present in a
"Guardian" install:

| Present in the install | Evidence | What Guardian claims |
|---|---|---|
| `drafting.py` — full draft composition | `src/inbox_zero_connector/drafting.py` | no drafting |
| `voiceprofile.py` — voice derivation | `src/inbox_zero_connector/voiceprofile.py` | no voice |
| **`voiceprofile.py:350 def save()`** — writes profiles to disk, `json.dumps` at `:359`, file write at `:369` | verified | **"storage-minimal"** |
| `classifiers/openai_compatible.py` — the LLM classifier | present | *"no model in the classification path at all"* (internal sprint plan) |
| `openai` client library | `pyproject.toml:32`, unconditional | as above |
| `server.py:798 create_draft` MCP tool | verified | no drafting |

That last row is the one that fails a security review. The Guardian claim in the
plan is *"in Guardian, classification never leaves the machine"*
(internal sprint plan). Today that is enforced by which classifier the
config names. A reviewer asked "what stops the model path running?" gets the
answer "a config value" — and the honest follow-up is that the same install,
one config edit away, calls out to an LLM with message content. The
`create_draft` posture hole above means it is not even one edit away on the MCP
surface: `drafts_enabled` alone reaches it.

Compare the no-send answer to the same reviewer: "there is no method." That
answer ends the conversation. The Guardian answer starts one.

### Is the gap real?

**Yes, and it is not close.** The posture mechanism is a good runtime narrowing
control and it is not what the plan asked for. The plan asked for absence and
named its own worked example; the mechanism delivers configuration. The
distinction is exactly the one the plan draws, so this is not an outside standard
being imposed — it is the plan's standard, unmet by the current build.

**The honest scale of the gap:** it is a gap in the SHIPPING MECHANISM, not in
the code's structure. Q3 shows the seams are unusually clean. Nobody has to
untangle anything; somebody has to decide how a second wheel gets built. That is
a smaller problem than "the AI is woven through the base," which is what this
usually turns out to be.

---

## Q3 — The seams, and how deep the coupling runs

### Import evidence, RUNNING

```
rg -n "drafting" src/inbox_zero_connector/            -> importers below
rg -l "voiceprofile" --glob '*.py' src/inbox_zero_connector/
```

| Module | Imported by | Line |
|---|---|---|
| `voiceprofile.py` | `drafting.py` only | `drafting.py:119` |
| `drafting.py` | `watcher.py` only | `watcher.py:74` |
| `drafting.py`/`voiceprofile.py` | `cli.py` | **never** — verified, `rg -n "drafting\|voiceprofile" cli.py` returns only two prose hits in help text, `cli.py:147` and `cli.py:437` |
| `drafting.py`/`voiceprofile.py` | `server.py` | **never** — `server.py:79-103` imports `corrections, ledger, scopes, config, providers, taxonomy, utils.logging_config`. No drafting import. |

This is a **near-perfect seam.** The AI tier is a two-module leaf
(`drafting.py` -> `voiceprofile.py`) with exactly one importer in the whole
package.

### The one attachment point, and it is unconditional

**`watcher.py:74`: `from inbox_zero_connector import drafting, learned, ledger`
— module level, unconditional.**

Consequence, stated as the brief asks: **deleting `drafting.py` from an install
today breaks `watcher.py` at import, which breaks the `watch` subcommand
(`cli.py:236 _cmd_watch`), which is the base kit's filing loop.** The base kit
does not survive naive removal of the AI tier.

How deep is that coupling really? Shallow. The usages are:
`watcher.py:222` (an optional field annotated `drafting.DraftBodyBackend | None`,
defaulting `None`), `watcher.py:621`, `:630`, `:684`, `:692` — all inside
`_draft` (`watcher.py:591`) and its helper `_thread_context` (`watcher.py:684`).
One function and its helper. `watcher.py:618` already short-circuits the whole
limb when no backend is injected, and `watcher.py:607-608` states the design
intent: *"filing is the product and drafting is the addition."*

So the correct summary is: **the coupling is one import line and one function,
and the architecture already treats drafting as optional — but the import
statement itself does not, so removal is a real (small) refactor, not a file
deletion.**

`server.py` is the good news case: `create_draft` composes nothing. It accepts a
body the host AI wrote and hands it to the provider. That is why `server.py`
needs no drafting import — and it is also why the MCP surface cannot be secured
by removing `drafting.py`. See the `create_draft` finding in Q1.

### What belongs to which kit

**`cli.py` subcommands** (verified against `cli.py:51-418`):

| Subcommand | Kit |
|---|---|
| `auth`, `logout`, `init-config`, `init-taxonomy`, `show-scopes`, `paths` | base — all three |
| `watch`, `reset` (`snapshot`/`restore`/`cursors`), `rules list`/`rules disable`, `digest` | base |
| `serve` | base |

**Nothing in the CLI is AI-tier-only.** `init-config` carries drafting FLAGS
(`cli.py:437` help text) but the command itself is base.

**`server.py` MCP tools** (verified, `@mcp.tool()` decorators):

| Tool | Line | Kit |
|---|---|---|
| `list_inbox` | `server.py:299` | base |
| `survey_inbox` | `server.py:337` | base |
| `get_thread` | `server.py:420` | base |
| `file_message` | `server.py:658` | base |
| `record_correction` | `server.py:715` | base (rule ENGINE is base per plan `README.md:137`) |
| `mark_important` | `server.py:765` | base |
| `get_daily_digest` | `server.py:926` | base |
| `read_sent_sample` | `server.py:496` | **AI tier** — samples sent mail to derive voice |
| `get_voice_routing` | `server.py:604` | **AI tier** |
| `create_draft` | `server.py:798` | **AI tier** |

Three of ten tools are AI-tier and **all three live in `server.py` alongside the
seven base ones.** This is the second real seam problem, and it is worse than the
`watcher.py` one because it is not one import line — it is three decorated
functions and their helpers (`_reply_recipients:479`, `_sent_sample_payload:572`,
`_voice_routing_payload:619`, `_validated_draft_request:883`) interleaved in a
single 940-line module. Absence for the MCP surface means either splitting the
module or conditionally registering tools, which is configuration again.

### Could the AI tier be removed from an install today without breaking the base?

**No — but the distance is short and measurable.** Three things block it:

1. `watcher.py:74` imports `drafting` unconditionally. Cost: move the import
   inside `_draft`, or move `_draft` behind a protocol the base does not name.
   One function.
2. `server.py` registers three AI-tier tools in the same module as seven base
   tools. Cost: extract them to a module registered conditionally, or split
   `server.py`. Meaningful but bounded.
3. `pyproject.toml:32` makes `openai` unconditional. Cost: an extra. Trivial
   mechanically.

Nothing here is entangled logic. It is three shipping-shape problems.

---

## Q4 — Options for producing three installs from one codebase

All four are PROPOSED. Costs are my estimate, not measured.

### Option A — runtime config only (the status quo)

**Absence: none.** Every capability ships; config declines to use it.

- Build cost: zero, it exists.
- Maintain cost: low mechanically, but it accrues a documentation debt — every
  Guardian claim must be phrased as a promise, and the two posture holes found in
  Q1 show how a config-only gate erodes without anyone noticing.
- Fails a client security review at the question *"what stops it?"*

### Option B — extras in `pyproject.toml` (`inbox-zero-connector[ai]`)

**Absence: partial and honest about being partial.** The `openai` dependency
genuinely disappears; the `drafting.py` and `voiceprofile.py` MODULES still ship
in the wheel because hatchling packages `src/inbox_zero_connector`
(`pyproject.toml:49-50`) as a unit.

- Build cost: small — one `[project.optional-dependencies]` block, plus fixing
  `watcher.py:74` so the import does not fail without the extra.
- Maintain cost: low. Standard Python, every packager understands it.
- Reviewer answer: "the LLM client is not installed." Better than config, weaker
  than "no method." A determined operator `pip install openai` away.

### Option C — an entry point that never imports the AI layer

**Absence: of REACHABILITY, not of code.** A `inbox-zero-guardian` console
script whose import graph provably excludes `drafting`/`voiceprofile`.

- Build cost: medium — a second entry point, a second `server.py` tool
  registration set, plus the `watcher.py:74` fix.
- Maintain cost: **medium-high and this is its real problem** — the guarantee is
  "this import graph excludes X," which nothing enforces unless a test asserts
  it. Without that test it silently regresses the first time someone adds a
  convenience import. With that test it is genuinely strong, and the test is
  cheap to write.
- Reviewer answer: "the Guardian binary's import graph does not reach the
  drafting module, and here is the test that proves it."

### Option D — build-time file selection in the installer

**Absence: total.** `drafting.py` and `voiceprofile.py` are not in the Guardian
wheel.

- Build cost: high — a build matrix, two wheels, two `pyproject.toml` targets or
  a hatch build hook, and a partitioned `server.py`.
- Maintain cost: **highest.** Two artifacts to test, two to version, two to
  release. Every change asks "which wheels?" A Guardian-only bug becomes
  invisible to the standard test run.
- Reviewer answer: the strongest available. `rg drafting` on the installed
  package returns nothing.

### Option E — separate distributions (found while assessing the others)

Extract the deterministic core to `inbox-zero-connector` and ship the AI tier as
`inbox-zero-enhancements`, a separate package that plugs in.

- **Absence: total, and it matches what `SPEC.md:20` already anticipates** —
  *"Technical identifiers stay `inbox-zero-connector` until the Guardian is
  extracted for reuse."* The extraction is already contemplated in the spec.
- Build cost: highest of all — two repos or a monorepo with two projects, a
  plugin boundary, a stable interface between them.
- Maintain cost: high, but it is the honest cost of a real product boundary
  rather than accidental cost. It is also the only option that makes Guardian
  and Enhancements the INDEPENDENT AXES the plan says they are
  (internal sprint plan), rather than one wheel with things switched off.
- Correct endpoint, wrong first move. It should not be built before a client has
  used either tier.

### RECOMMENDATION — PROPOSED, for rTech's decision

**Ship v1 on Option A (status quo config), and spend the small budget on C's
enforcement test rather than on A's documentation debt.** Concretely, three
moves in priority order:

1. **Fix the two posture holes first, independent of any kit decision.**
   `server.py:906` should call `drafts_allowed`, not `drafts_on`, so posture
   actually narrows the MCP surface; `watcher.py:621` should route through
   `may_draft`. These are correctness bugs in the mechanism that already exists
   and they cost almost nothing. A Guardian claim of any kind is worthless while
   the MCP surface bypasses the gate.
2. **Then take Option B (extras).** It is the cheapest thing that converts any
   part of the claim from promise to guarantee — `openai` genuinely absent — and
   it forces the `watcher.py:74` fix that every other option also needs. Small,
   reversible, no architecture committed.
3. **Write the import-graph test from Option C now, even on Option A.** A test
   asserting `drafting` is not reachable from the base entry point costs an
   afternoon and is what makes C or D cheap later. It is also the artifact you
   hand a reviewer.

**Why not D or E for v1:** the plan's own reasoning applies —
*"building three postures before David has seen one is speculative"*
(internal sprint plan). D and E are irreversible shape decisions made
before a single client has used the thing. Do them when a client's security
review demands them, and let the review specify what it needs.

**Why not stop at A:** because A's cost is not zero, it is deferred and it lands
in a client conversation rather than in a sprint. The two posture holes are
evidence of exactly that — a config-only gate degrades without a failing test to
notice.

**The claim that must NOT be made in the meantime:** Guardian as a build-level
guarantee. Until at least B lands, Guardian is honestly described as *"a
restricted configuration of the audited connector, with the no-send guarantee at
the code layer"* — which is true, defensible, and still strong, because the
no-send absence is real. Selling the rest of it as absence today is the claim
that fails.

### Naming — UNDECIDED, twice over

The internal sprint plan marks "rTech Enhancements" as *(working name)* and
it also says *"The final name for Enhancements is deferred — it is a marketing
decision."* This document invents nothing.

And per section 0, "Guardian" itself is now overloaded between the connector's
product name (`SPEC.md:18`) and the secure tier (internal sprint plan).
**Two naming decisions are open, not one.**

---

## Summary of findings outside this lane's writable set

Reported with evidence, not fixed, per the lane contract.

1. **`server.py:906` gates `create_draft` on `drafts_on`, not `drafts_allowed`.**
   Posture does not narrow the MCP draft surface. `config.py:576` documents
   `drafts_allowed` as *"the only sanctioned way to ask the question"*; the MCP
   tool does not ask it. WRITTEN-level.
2. **`watcher.py:621` calls `should_draft`, not `may_draft`,** skipping the
   deployment question. Currently unexposed because nothing injects a
   `draft_backend` in production (`rg -n "draft_backend=" src/ tests/ scripts/`
   -> one hit, `tests/test_watcher_drafting.py:130`), so this is latent, not
   live. It becomes live the moment the watcher is wired.
3. **`watcher.py:74` imports `drafting` unconditionally at module level**,
   coupling the base filing loop to the AI tier at import time.
4. **"Guardian" is overloaded** between `SPEC.md:18` (product name of the whole
   connector) and the internal sprint plan (restricted tier).
   `CAPABILITIES.md:949` already uses the tier sense inside the kit docs.
5. **`voiceprofile.py:350 save()` is a storage writer** shipping in every
   install, against Guardian's "storage-minimal" claim.
