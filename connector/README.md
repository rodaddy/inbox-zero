# rTech Email Guardian

**Status: 0.2.15. Code RUNNING — verified against live mailboxes (see below).
Beta installs MERGED and shipping; a stranger install is WRITTEN, not yet
proven** (that last one is [INSTALL-RUNBOOK.md](INSTALL-RUNBOOK.md), which
states the same three words). Stage 1 (triage) and Stage 2
(voice + reply drafts) are written and covered by the local suite. Both zip
tiers install and run: 0.2.9 fixed the basic tier, which previously failed to
import because watch-core loaded the voice add-on eagerly
([CHANGELOG.md](CHANGELOG.md), #146).

Proven against live mailboxes: Gmail end-to-end (sign-in, watch pass, filing,
receipts) on 2026-08-18, and Microsoft 365 sign-in plus a read pass against a
live tenant on 2026-08-23.

Not yet proven: the clean-room stranger install (someone who has never seen
this repo, on an unseen machine, working from
[INSTALL-RUNBOOK.md](INSTALL-RUNBOOK.md) alone), and Microsoft 365 filing
end-to-end on a customer tenant. Treat those two as untested until a
transcript says otherwise — 0.2.9's Graph fixes (the move-id refile loop,
paged change feeds, the unknown-id move receipt) are covered by the local
suite, not by a live-tenant filing transcript.

An MCP server that gives a host AI (Claude Desktop, Codex) least-privilege
access to a Gmail or Microsoft 365 mailbox: it can **read, file, flag, sample
sent mail, and write reply drafts**. It cannot send, delete, or forward, and it
cannot address a draft to anyone who is not already on the thread.

Determinism-first: this connector owns every mailbox action. The AI decides
*which* message goes *where* and writes the words of a reply; the code performs
the move, validates it against a closed folder vocabulary, picks the voice from
a deterministic rule table, and receipts everything to an append-only ledger.
The AI never touches a mail API and never writes the ledger.

## The no-send claim, stated exactly

The honest version, which is the one that survives a security questionnaire —
and it is **now configuration-dependent on Gmail**, so state which posture the
deployment is in rather than reciting a single sentence.

> **Microsoft, always:** the OAuth token this connector holds is not accepted
> by Graph's send API. Sending is refused by Microsoft's servers, not by our
> code.
>
> **Gmail, on the shipped interactive sign-in, drafts on or off:** the token
> *could* send, and does not, because this connector contains no send code
> path. That is a code-layer guarantee, asserted by two independent scanners on
> every build — not a structural one.

| Provider | Auth mode | Drafts | Requested scopes | Enforcement |
| --- | --- | --- | --- | --- |
| Google / Gmail | interactive (**shipped**) | off or on | `gmail.modify` | **code-layer** |
| Google / Gmail | domain-wide delegation | off | `gmail.readonly`, `gmail.modify.restricted` | **scope-layer** |
| Google / Gmail | domain-wide delegation | on | the pair above **plus `gmail.compose`** | **code-layer** |
| Microsoft 365 | browser (**shipped**) | off or on | `Mail.ReadWrite` | **scope-layer** |

**Read the first row, not the second.** Interactive consent is the only Gmail
mode this kit currently runs. `gmail.modify.restricted` is documented by Google
solely for the domain-wide-delegation path, and a consent-flow request for it is
refused with `Error 400: invalid_scope` — verified against a live consent
screen on 2026-08-18. Until the DWD service-account flow is implemented,
**every shipped Gmail install is code-layer**, whatever the drafts flag says. The DWD rows are
what the posture becomes once that lands, and they are here so the contrast with
Microsoft stays visible.

### Why Gmail cannot give the structural claim today

Google's `users.drafts.create` and `users.drafts.send` accept an **identical**
scope set (`mail.google.com`, `gmail.modify`, `gmail.compose`). There is no
compose-without-send scope, so "can create a draft but cannot send it" is not
expressible as a Gmail token. Verified 2026-08-17 against the per-method
"Authorization scopes" section of each reference.

Microsoft has no such problem: `Mail.ReadWrite` covers `createReply` and is
refused by both send paths, so **Graph's posture is unchanged by Stage 2**.

On the shipped interactive flow the guarantee rests on two walls, neither of
them the token — and turning drafts off does not add a third, because
`gmail.modify` is requested either way:

1. **Code** — no send code path exists. Asserted by
   `tests/test_security_surface.py`, which ships with the kit, and by a second
   repo-internal lexical scanner that runs on every rTech build.
2. **Tenant** — Workspace per-app access control denying send to this app.
   With the shipped Gmail flow this is the wall a buyer can verify from their
   admin console, so treat it as a required onboarding step rather than an
   optional extra.

Verify the running configuration yourself at any time. This prints the **live**
posture computed from the installed config, not a generic table:

```bash
uv run inbox-zero-connector show-scopes
```

Scope strings are defined in exactly one file,
[`services/auth/scopes.py`](src/inbox_zero_connector/services/auth/scopes.py),
with the doc citation for each. `tests/test_security_surface.py` fails the
build if a send-capable scope string or a send/delete endpoint literal appears
anywhere in executable source.

### Citations

Verified 2026-08-17 against the per-method "Authorization scopes" sections of
the vendor REST references, not summary or guide prose.

- Gmail send accepts exactly four scopes, none of which we request:
  <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send>
- `gmail.modify.restricted` is accepted by `messages.modify` / `batchModify`:
  <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify>
- Gmail scope list: <https://developers.google.com/workspace/gmail/api/auth/scopes>
- Graph permissions reference (states the `Mail.ReadWrite` send exclusion verbatim):
  <https://learn.microsoft.com/en-us/graph/permissions-reference>
- Graph send paths require `Mail.Send`, higher-privileged "Not available":
  <https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0> ·
  <https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0>

### Caveats we do not hide

These are real and belong in front of a technical buyer, not buried.

1. **"Cannot send" is not "narrowly scoped."** Graph's `Mail.ReadWrite` is
   whole-mailbox read/write; Graph has no folder-scoped OAuth permission at
   all. Gmail's pair is genuinely narrow. Claim the send property, not a
   general minimality property, on Microsoft.
2. **Delegated consent is dynamic on Microsoft.** Omitting `Mail.Send` from
   the app registration is not by itself an unbypassable barrier — the same
   client could request a different token that includes it. Correct phrasing
   is *"the token this kit uses cannot send"*, never *"sending is
   impossible."* To make it structural, harden the tenant (Entra consent
   policy, Exchange `ApplicationAccessPolicy`).
3. **`gmail.modify.restricted` is a restricted scope and, per Google's own
   `messages.modify` documentation, the domain-wide-delegation path** — a
   Workspace admin arrangement with a service account, not a plain end-user
   consent flow. It also requires restricted-scope OAuth verification, and a
   third-party security assessment if data is stored or transmitted
   server-side. This connector stores nothing server-side and transmits
   nothing to rTech.
4. **Gmail is code-layer on every shipped install, drafts on or off.** Stage 1
   predicted the drafts downgrade, and a live consent screen on 2026-08-18 then
   showed that the drafts-off posture was never scope-layer either on the
   interactive flow: `gmail.modify.restricted` is DWD-only, so interactive
   consent requests bare `gmail.modify`, which Gmail's send methods also
   accept. Turning drafts off does NOT buy back a structural claim on Gmail.
   Microsoft is scope-layer either way; if a buyer needs the structural claim,
   that is the provider to put them on.
5. **Client-side scope omission is defense in depth, not the boundary.** The
   boundary is the provider's server-side scope check plus tenant consent
   policy.
6. **A voice profile is built from the owner's own sent mail.** That is
   sensitive by construction, which is why `read_sent_sample` requires an
   explicit selection and why the profile must describe patterns and never
   quote excerpts, client names, or matter details (invariant 5).

## What it exposes

Exactly ten MCP tools. The tool surface is the attack surface.

**Ten tools, eleven provider operations — the two counts are different on
purpose.** The ten below are what an AI host can call. The provider interface
underneath carries one operation no tool exposes: `create_self_draft`, reached
only by the human typing `digest --as-draft` (see [The digest reaches you as a
draft](#the-digest-reaches-you-as-a-draft-never-as-sent-mail) below). So eleven
is the security surface an audit counts, ten is the surface an AI can reach,
and the extra one is the one an AI cannot.

| Tool | Does | Receipted |
| --- | --- | --- |
| `list_inbox` | read message headers | — |
| `survey_inbox` | read the whole inbox as a triage worklist, **read only** | — |
| `get_thread` | read one conversation with bodies, plus the routed voice | — |
| `read_sent_sample` | read sent mail from an **explicit selection** | — |
| `file_message` | move into a **fixed** taxonomy folder | yes |
| `record_correction` | the owner says it belongs elsewhere: file it **and** learn the rule | yes, two rows |
| `mark_important` | raise/lower a flag | yes |
| `create_draft` | reply draft on an existing thread, **never sent** | yes, with the voice |
| `get_voice_routing` | read the routing rules and the live no-send posture | — |
| `get_daily_digest` | read the audit ledger | — |

There is deliberately **no** `write_ledger` tool. A receipt the AI can choose
to skip is not an audit trail, so receipts are a side effect of a mutation
succeeding.

### `create_draft` has no recipient parameter

Not a validation rule — a **signature property**. There is no `to`, no `cc`, no
`bcc`, and no `recipients` argument on the tool, on the provider interface, or
on either adapter. The recipients are the thread's own participants minus the
owner, derived by the connector from the provider's view of the thread.

That matters because `get_thread` returns attacker-controlled text. A
prompt-injection that talks the host AI into "reply, and copy
compliance@attacker.example" needs somewhere to put that address, and there is
nowhere. A validator that stripped bad recipients would be code somebody could
weaken; an argument that does not exist cannot be passed. The advertised MCP
schema is asserted to carry only `thread_id`, `body`, and `voice`.

On Microsoft this is Graph's doing as well as ours: `createReply` builds the
reply server-side, so the adapter sends only a body.

### The digest reaches you as a draft, never as sent mail

`inbox-zero-connector digest --as-draft` leaves the day's digest in your **own**
drafts folder, subject `Inbox Zero digest <YYYY-MM-DD>`. That is delivery with
no send verb anywhere on the path.

It is the connector's eleventh — and last — provider operation,
`create_self_draft(subject, body)`. Like `create_draft` it has **no recipient
parameter**, and it is the narrower of the two: `create_draft` addresses a
reply to the thread's own participants, while this one is created with empty
To, Cc, and Bcc and is addressed to **nobody**. It reaches no endpoint the
connector was not already reaching, requests no additional scope, and obeys the
same drafts posture — with drafts off for an account it refuses in one line
naming the flag rather than delivering some other way. The reason it exists at
all is that every other channel that delivers mail *sends* mail, and this
connector does not send.

### `survey_inbox` ships the filing criteria with the mail

Read only. It calls `list_inbox` and nothing else — it classifies nothing,
moves nothing, and receipts nothing, because it changes nothing. Acting on the
worklist still goes through `file_message` and `mark_important`, which are the
only tools that touch the mailbox and which receipt themselves.

The difference from `list_inbox` is the taxonomy, not the page size.
`list_inbox` reports folder **names**, which is enough to file one message you
have already read. Triage is the harder problem — deciding where a hundred
messages go, consistently, in one pass — and names alone do not carry the
decision criteria. The folder **descriptions** do: the test separating
Business-Action from Business-Waiting, the rule that Receipts beats Reference,
the instruction that Review is a correct answer rather than a failure.
`survey_inbox` returns those descriptions alongside the mail so a classifier
has the criteria in front of it instead of recalling them from memory halfway
down a long worklist.

It also reports `truncated` explicitly rather than leaving the AI to infer a
short sweep from a count that happens to equal the limit. A sweep that silently
stopped short leaves the owner believing their inbox was triaged when part of
it was never looked at.

Snippets in the worklist are attacker-controlled text, exactly like thread
bodies, and nothing read there can widen the folder set — `file_message`
validates against the closed taxonomy regardless.

### `read_sent_sample` requires a selection

Selected sources, not scraped. At least one of `labels`, `after`, `before`, or
`message_ids` is required; a call with none of them is **refused**, not
interpreted as "everything". `exclude_labels` always wins over the inclusions.

There is no argument-free call path that reads a whole sent folder, and that is
data minimisation as a security property rather than a nicety — "learn my
voice" over an unbounded sent folder means reading every confidential thing the
owner ever wrote.

### The voice is chosen by the connector, never by the AI

Deterministic, in this order:

1. every recipient domain == the owner's domain → **internal**
2. the thread carries a configured sales label → **sales**
3. anything else, and **every ambiguous case** → **external**

Ambiguity — no recipients, unparseable recipients, or a thread mixing internal
and external people — defaults to external, because external is the most
guarded register. The failure mode is being slightly too formal with a
colleague rather than too familiar with a stranger.

`resolve_voice` is a pure function of recipient domains and thread labels. It
takes no message body, so no injected text can reach it. Domain matching is
exact: `mail.example.com`, `evil-example.com`, and
`example.com.attacker.test` are all external. The ledger records which voice
each draft used, and the daily digest breaks drafts down by voice — which is
how an owner notices a mis-tuned routing table without opening a draft.

## Security invariants

1. **No send capability exists.** Not blocked — absent. No send/delete/forward
   method on the interface or either adapter, no code path reaching a send or
   delete endpoint, and no recipient parameter anywhere. Asserted mechanically.
2. **Email content is untrusted input.** `file_message` validates against a
   closed taxonomy *before* any API call, so a hostile email that talks the AI
   into a bad label gets rejected at zero API cost. Worst case is a message
   misfiling itself into a folder the owner already knows about. For drafts the
   worst case is a badly written reply to people already on the thread, which
   the owner reads before sending — because only the owner can send.
3. **Nothing destructive.** Every mutation is a reversible label/move/flag, or
   an unsent draft. Filing removes the `INBOX` label (Gmail) or moves the
   message (Graph) — never trash, never delete.
4. **Everything is receipted.** Every mutation appends one JSONL row, written
   by the connector with `fsync`. The ledger records actions only — message
   ids, folders, timestamps, and for a draft the **voice name** — never
   subjects, bodies, or addresses, so a leaked ledger is not a leaked mailbox.
5. **Voice profiles leak nothing.** `read_sent_sample` reads only the owner's
   explicit selection, and the profile it feeds must describe patterns —
   greetings, sign-offs, cadence, formality — never excerpts, client names, or
   matter details. The tool returns that rule alongside the messages so it
   reaches the model that writes the profile, not just a document.
6. **Their tenant, their token.** rTech hosts nothing and holds nothing.
   Tokens live in the OS keychain via `keyring`; nothing token-like is written
   to any file or log. On the Microsoft side the kit ships rTech's own
   MULTI-TENANT PUBLIC CLIENT so a buyer need not register anything — a public
   client carries no secret, consent is granted in the buyer's own tenant, and
   the token is minted straight to the buyer's machine, so rTech is still not
   in the token chain. A buyer who prefers the registration inside their own
   tenant overrides `microsoft_client_id` and is otherwise unaffected. Google
   remains bring-your-own, because its desktop flow requires a client SECRET
   and a shipped secret would be a real one.
7. **Drafts stay drafts.** The human is always the sender. The connector cannot
   send one, and the copy says so proudly: the owner gets their inbox time back
   and remains the correspondent (extend-not-replace).

## Install

Requires Python ≥3.14 and [uv](https://docs.astral.sh/uv/).

```bash
cd connector
uv sync
uv run inbox-zero-connector init-taxonomy   # writes the default folder set
uv run inbox-zero-connector paths           # where config and ledger live
```

### Configure drafts and the voice routing table

Stage 2 needs one more install step. **Drafts are off by default on both
providers**, so a deployment that skips this step behaves exactly like Stage 1.
On Microsoft that also means no send-capable scope is ever requested; on the
shipped Gmail interactive flow `gmail.modify` is requested regardless.

```bash
# Triage only (Microsoft scope-layer; Gmail code-layer -- see the table):
#   do nothing -- the default config is drafts-off.

# Drafts on Microsoft only (Microsoft stays scope-layer):
uv run inbox-zero-connector init-config \
  --owner-domain example.com \
  --enable-microsoft-drafts \
  --sales-label Prospects

# Drafts on Gmail (Gmail is already code-layer; this adds draft writing):
uv run inbox-zero-connector init-config \
  --owner-domain example.com \
  --enable-google-drafts
```

`--owner-domain` is required: the internal-vs-external rule is meaningless
without it. `--sales-label` is repeatable. Voices default to `internal`,
`external`, `sales` and the installer may extend the list by editing
`connector-config.json`; every named role is validated against that list at
load, so a typo fails at install rather than on the first customer-facing
draft.

On the **domain-wide-delegation** flow, enabling Gmail drafts adds
`gmail.compose`, so re-run `auth google` after — the existing token does not
carry it. On the **shipped interactive flow the requested scope set does not
change**: `gmail.modify` already covers `drafts.create`, so no re-consent is
needed and no new scope appears. `show-scopes` prints the set this install
actually requests.

`requirements.txt` is exported from `uv.lock` for environments without uv:

```bash
uv export --no-hashes --no-dev --no-emit-project -o requirements.txt
```

### Register an OAuth client in your own tenant

**Google.** Create an OAuth client (Desktop app) in your own Google Cloud
project, enable the Gmail API, then:

```bash
export INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_ID=...
export INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_SECRET=...
uv run inbox-zero-connector auth google
```

Note caveat 3 above: `gmail.modify.restricted` is documented as the
domain-wide-delegation path, so a Workspace deployment is the supported
arrangement. Confirm this with your Workspace admin before rollout.

This consent requests `gmail.modify`, which is send-capable, whether or not
drafts are enabled (the DWD flow is the one where drafts add `gmail.compose`).
Deny send to this app in Workspace per-app access control as part of
onboarding — on the shipped Gmail flow that tenant control is a load-bearing
wall, not a nice-to-have.

**Microsoft — optional.** The kit already ships rTech's multi-tenant public
client, so the default path is `uv run inbox-zero-connector auth microsoft`
with no registration at all; a business tenant grants admin consent for
`Mail.ReadWrite` once (INSTALL-RUNBOOK.md step 4).

To use your OWN registration instead, register an application in Entra ID
(public client, redirect URI `http://localhost` under the mobile & desktop
platform — the browser PKCE flow is the only one Entra security defaults
allow), add the delegated `Mail.ReadWrite` permission and *nothing else*,
then override the shipped identifiers:

```bash
export INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID=...
export INBOX_ZERO_CREDENTIALS__MICROSOFT_TENANT_ID=...      # optional, defaults to "common"
uv run inbox-zero-connector auth microsoft
```

Tokens are stored in the OS keychain. Remove one with
`uv run inbox-zero-connector logout google|microsoft`.

## Claude Desktop configuration

The connector is a **local stdio MCP server**, so it installs as a Claude
Desktop local MCP server / desktop extension — *not* as a claude.ai "custom
connector," which is remote-MCP-only and would require a public HTTPS endpoint.
Nothing about this connector traverses Anthropic's cloud.

```json
{
  "mcpServers": {
    "inbox-zero": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/inbox-zero/connector",
        "run",
        "inbox-zero-connector",
        "serve"
      ],
      "env": {
        "INBOX_ZERO_PROVIDER": "google",
        "INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_ID": "...",
        "INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_SECRET": "..."
      }
    }
  }
}
```

Set `INBOX_ZERO_PROVIDER` to `google` or `microsoft`.

**Team/Enterprise rollout gate — raise this with IT before selling the kit.**
Two independent controls can block a local MCP server, and machine policy wins
over anything an org owner sets in-app:

- **In-app:** if an Owner enabled the desktop-extension allowlist
  (Organization settings → Connectors → Desktop), users cannot side-load an
  `.mcpb` at all; an Owner must upload it there first. Requires Claude Desktop
  ≥ 0.13.91. Note that enabling that allowlist force-deletes existing
  extension installs, so it is not a no-op toggle.
- **Machine policy (overrides in-app):** if IT deployed
  `isLocalDevMcpEnabled: false` or `isDesktopExtensionEnabled: false` via MDM
  (macOS `com.anthropic.claudefordesktop`) or Group Policy/Intune
  (`HKLM\SOFTWARE\Policies\Claude`), nothing an Owner does re-enables it.

If the buyer *is* the Owner and there is no MDM, this is a ten-minute path. On
an MSP-managed fleet, confirm those two policy keys first — it is a
device-management change, not a Claude setting.

## Development

```bash
uv run pytest        # the full suite; no network or credentials required
uv run ruff check .
uv run mypy src
```

Tests cover taxonomy enforcement, ledger append-only behaviour and the digest,
the voice routing table, selection enforcement on sent-mail reads, both provider
adapters against mocked transports, the MCP tool layer, and
`test_security_surface.py`, which asserts the invariants mechanically. That
scanner strips comments and docstrings before searching, because
`services/auth/scopes.py` and `providers/base.py` deliberately *document* the
endpoints they refuse to call — documenting the boundary is required; crossing it is the defect. Two
tests assert the scanner both catches a planted violation and ignores a
documented mention, so it cannot pass vacuously.

### Two deliberate spellings in the source

Both look like oversights and neither is; both exist to keep the scanners
strict instead of loosening them for a legitimate call.

- **`GMAIL_COMPOSE` is defined by indexing into `GMAIL_SEND_WOULD_REQUIRE`**,
  not by restating the string. That is the honest shape of the fact — the only
  Gmail scope that can create a draft is a member of the set that can send one
  — and it means enabling drafts adds no send-scope literal anywhere.
- **The Gmail adapter reaches its one drafts call by name** (`_DRAFTS_RESOURCE`
  / `_CREATE_METHOD`) rather than as `drafts().create(...)`. Gmail's create and
  send methods differ only in the final word, so a lexical scanner that
  permitted `drafts.create` would be one typo from permitting `drafts.send`.
  The call is not hidden: it is a named constant, documented in the module
  docstring and here, and two tests assert both that the indirection is intact
  and that no `drafts.send` spelling exists.

`_graph/done-means/inbox-zero-no-send.sh` is the second, independent scanner —
a standalone lexical check with no Python dependency, so a hook, CI, or a cold
agent can run it against a tree that does not import. It lives in rTech's
source repo and is **repo-internal: it is not shipped in this distribution**.
`tests/test_security_surface.py` is the scanner you have here.

## Stage 2 — what landed

`read_sent_sample`, `create_draft`, `get_voice_routing`, the voice routing
table, and the per-provider `drafts_enabled` flag. The Gmail scope downgrade
that Stage 1 predicted did happen and is documented above rather than
smoothed over.

**Voice profiles ARE built and shipping** — the `voice` CLI tree
(`voice build|show|approve`, with sent-mail selection by
`--sent-label`/`--sent-after`/`--sent-before`/`--sent-limit`, or
`--from-corpus DIR`) is in this build; a profile is unusable until `voice
approve` raises the approved flag. See INSTALL-RUNBOOK.md § 7c.

Still not built: the `voice-profile` and `draft-replies` **kit skills** — the
packaging layer that would drive that CLI tree from inside a chat host — and
the clean-room install test. The CLI path they would wrap already works.
`INSTALL-RUNBOOK.md` shipped and is the ordered install path.
