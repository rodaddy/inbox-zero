# Inbox Zero — Posture Runbook (grant-discovery model)

**Audience: the MSP owner.** You are already a tenant admin for every client you
manage, you do app registrations and consent grants routinely, and you are going
to run this against a real law firm's tenant. This runbook assumes Entra and
Google Admin fluency and does not explain either. It is exact because a wrong
permission name in a runbook gets pasted into a production tenant.

**Version:** 0.1.0 · **Status: PROPOSED.** The grant-discovery layer this
document describes — `connector/src/inbox_zero_connector/grants.py` — **does not
exist on disk as of 2026-08-18.** It is being written in a parallel lane. The
posture *model* below is the operator's decided architecture; the *behaviour*
described in sections 1, 4, and 5 is a design, not an observed run. Every
Microsoft Graph and Gmail permission name in sections 2 and 3 IS verified
against vendor documentation this session, with citations — those you can paste.

What exists today, WRITTEN and test-covered, is
`connector/src/inbox_zero_connector/scopes.py`, which is the *previous*
architecture: a fixed, capability-limited scope request. That module is the
security manifest the current shipped code uses. Where this runbook and
`scopes.py` disagree, `scopes.py` describes what the code does now and this
runbook describes what it is being changed to.

---

## 1. The model

Three sentences, and they are the whole thing.

1. **Requested scopes are our ask.** The connector ships with full capability in
   code — read, file, draft, send, forward, rules — and asks for what a given
   posture needs.
2. **Granted scopes are the tenant's answer.** At authentication time the
   connector reads back what the token actually carries and configures itself to
   exactly that set.
3. **The granted set always wins.** A capability whose permission is absent is
   not degraded, not retried, and not worked around. It is not registered as a
   tool at all.

**The connector fails closed.** An unrecognized grant permits nothing. An absent
grant permits nothing. A grant the connector cannot parse permits nothing. There
is no default-open path and no "assume read at least" fallback — if grant
discovery itself fails, the connector starts with an empty capability set and
says so.

**What this means for you, said plainly: you decide what this software can do on
any given mailbox. rTech does not.** We do not hold a client ID, we are not in
the token chain, and we cannot widen a grant from our side. The ceiling is
whatever you consented to in your own admin console, and you can lower it or
raise it without telling us.

**You can verify this without trusting our code.** The grant is visible in your
own console (§4), the capability list the connector reports must match it, and
the negative check in §4.3 proves an absent capability is genuinely absent rather
than merely unused. A claim you can run beats a claim you have to believe.

One honest limit, stated here rather than buried: **delegated consent in Entra is
dynamic.** Leaving a permission off the app registration is not a permanent
prohibition — the same client could request a token that includes it, and a user
or admin would face a consent prompt. The defensible sentence is *"the token this
connector holds cannot do X."* Making it *"nothing can do X"* requires the
tenant-side controls in §5.4. Say it that way to a security reviewer; one who
discovers the distinction later will discount everything else you told them.

---

## 2. The three postures — Microsoft Graph

All permissions below are **delegated** (work or school account). Application
(app-only) permissions grant tenant-wide mailbox access and are **never** part of
any posture in this kit.

### 2.1 READ-ONLY — the law-firm default

Grant exactly:

```
Mail.ReadBasic
Mail.Read
```

`Mail.ReadBasic` is the documented least-privileged permission for
`GET /me/messages`; `Mail.Read` is listed as a higher-privileged alternative and
is what you need for message bodies. ([List messages][ms-list])

**Connector discovers:** `read_mail`, `classify`, `digest`.
Tools registered: `list_inbox`, `survey_inbox`, `get_thread`,
`read_sent_sample`, `get_daily_digest`.

**IMPOSSIBLE under this grant** — the list a security-conscious buyer actually
reads:

- **Cannot move, file, or archive a message.** `POST /me/messages/{id}/move`
  requires `Mail.ReadWrite` and lists no lower alternative. ([message: move][ms-move])
- **Cannot flag, categorize, or mark read.** `PATCH /me/messages/{id}` is a write.
- **Cannot create a draft.** `createReply` requires `Mail.ReadWrite`. ([message: createReply][ms-createreply])
- **Cannot send, reply, or forward.** All three require `Mail.Send`, with
  "Higher privileged permissions: Not available." ([message: reply][ms-reply], [message: forward][ms-forward])
- **Cannot create, read, or modify inbox rules.** Rules live behind
  `MailboxSettings.ReadWrite`, a permission from an entirely different family.
  ([Create rule][ms-rules])
- **Cannot delete anything.**

Nothing leaves the mailbox and nothing in it changes. The connector reads, forms
a judgment, and writes that judgment only into its own local ledger.

### 2.2 STANDARD — inbox zero with a human sending

Grant exactly:

```
Mail.ReadWrite
```

One permission covers the whole posture. Microsoft's own permissions-reference
description is, verbatim: *"Allows the app to create, read, update, and delete
email in user mailboxes. **Does not include permission to send mail.**"*
([Permissions reference][ms-permref])

**Adds:** `file_message` (move between folders), `mark_important` (flag,
categorize, mark read), `create_draft` (including reply drafts via `createReply`,
which lands a message with `isDraft: true` and sends nothing).

**IMPOSSIBLE under this grant:**

- **Cannot send, reply-and-send, or forward.** Every send path —
  `POST /me/sendMail`, `POST /me/messages/{id}/send`, `/reply`, `/forward` —
  requires `Mail.Send` and offers no higher-privileged substitute that
  `Mail.ReadWrite` satisfies.
- **Cannot manage inbox rules.** Still `MailboxSettings.ReadWrite`, still absent.
- **Cannot reach another user's mailbox.** That is `Mail.ReadWrite.Shared` or an
  application permission, neither of which is granted.

This is the strongest form of the kit's headline claim, and on Graph it is a
**scope-layer** guarantee: the refusal comes from Microsoft's servers, not from
our code. A draft sits in Drafts until a human presses Send.

**One thing to state honestly:** `Mail.ReadWrite` is **whole-mailbox**. Graph has
no folder-scoped mail permission — even the folder-scoped
`POST /me/mailFolders/{id}/messages` endpoint requires the full permission. Claim
"cannot send." Do not claim "narrowly scoped," because a reviewer will check.

### 2.3 FULL — the agency posture

Grant:

```
Mail.ReadWrite
Mail.Send
MailboxSettings.ReadWrite
```

**Adds:** `send_message`, `reply`, `forward`, and rules management
(`list_rules`, `create_rule`, `update_rule`).

`Mail.Send` is the least-privileged permission for `sendMail`, `reply`,
`replyAll`, and `forward`. `MailboxSettings.ReadWrite` is the least-privileged
permission for `POST /me/mailFolders/inbox/messageRules` — **not** a `Mail.*`
permission, which is the single most commonly mis-stated fact in this area.
([Create rule][ms-rules])

**Still IMPOSSIBLE, even here:**

- **Cannot permanently delete mail.** No delete capability is implemented in the
  connector under any posture; `Mail.ReadWrite` would technically permit it, and
  the connector does not expose it.
- **Cannot reach mailboxes other than the signed-in user's.** No
  `Mail.ReadWrite.Shared`, no application permissions.
- **Cannot change tenant-level configuration, transport rules, or other users'
  settings.** `MailboxSettings.ReadWrite` is the signed-in user's own mailbox
  settings only.
- **Cannot read or alter the Exchange Online Archive mailbox.** Separate mailbox,
  outside this permission set. (Marked INFERRED-excluded and UNVERIFIED in the
  kit's own research — see `CAPABILITIES.md` §2.1.)

Read §6.3 before granting this to a law firm.

### 2.4 Graph permission summary

| Capability | Delegated permission | Source |
| --- | --- | --- |
| List / read messages | `Mail.ReadBasic` (least), `Mail.Read` / `Mail.ReadWrite` (higher) | [List messages][ms-list] |
| Move / file a message | `Mail.ReadWrite` | [message: move][ms-move] |
| Create a reply draft | `Mail.ReadWrite` | [message: createReply][ms-createreply] |
| Send / reply / forward | `Mail.Send` (no higher available) | [reply][ms-reply], [forward][ms-forward] |
| Create / manage inbox rules | `MailboxSettings.ReadWrite` (no higher available) | [Create rule][ms-rules] |

All five rows fetched from Microsoft Learn on **2026-08-18** and quoted from each
page's own Permissions table, not from a summary. This matters: an AI
summarisation pass over the permissions reference once rendered `Mail.ReadWrite`
as *including* send, which is the opposite of the source text
(`scopes.py:GRAPH_MAIL_READWRITE`). Read the raw doc.

---

## 3. The three postures — Gmail

Google's vocabulary is not a translation of Microsoft's, and the asymmetries are
real. Every scope string below is quoted from Google's own scopes reference,
fetched 2026-08-18. ([Gmail API scopes][g-scopes])

### 3.1 READ-ONLY

```
https://www.googleapis.com/auth/gmail.readonly
```

Google's description: *"View your email messages and settings."* Classified
**restricted**.

**Connector discovers:** identical to Graph READ-ONLY —
`read_mail`, `classify`, `digest`.

**IMPOSSIBLE:** any label change (that is `gmail.modify` or
`gmail.modify.restricted`), any draft, any send, any filter change. This is a
clean parallel to Graph READ-ONLY with no asymmetry worth flagging.

### 3.2 STANDARD — and here the parallel breaks

**This is the honest asymmetry, and it is the most important paragraph in this
document for a Gmail client.**

Graph expresses "can file and draft but cannot send" as a single permission.
**Gmail cannot express that posture at all in an interactive consent flow.**

- `gmail.modify` — *"Read, compose, and send emails from your Gmail account."*
  It is **accepted by `users.messages.send`**. A token holding it can send.
- `gmail.compose` — *"Manage drafts and send emails."* Also send-capable.
  `users.drafts.create` and `users.drafts.send` accept an **identical** scope
  set, so any Gmail token that can put a draft in the Drafts folder can also send
  that draft. There is no compose-without-send scope in the Gmail API.
- `gmail.modify.restricted` — the one Gmail mutation scope that is **not**
  accepted by any send method. It permits label add/remove on messages
  (file, archive, star, mark read) and nothing else.

So there are two Gmail STANDARD variants, and they are not equivalent:

**STANDARD (DWD) — scope-layer, the strong one:**

```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify.restricted
```

Filing works; **drafting does not**, because no draft scope is present. This is
the only Gmail configuration where the no-send guarantee comes from Google's
servers rather than from our code.

**Two hard constraints on this variant, both verified:**

- **`gmail.modify.restricted` is DWD-only.** Google's `users.messages.modify`
  reference documents it solely for *"a service account that has domain-wide
  delegation authority to impersonate users."* ([users.messages.modify][g-modify])
  **PROVEN LIVE 2026-08-18:** an interactive consent request including this scope
  is refused with `Error 400: invalid_scope` before sign-in completes. It must
  come through a domain-wide delegation grant in Google Admin, never a consent
  screen.
- **The DWD service-account flow is NOT YET IMPLEMENTED** in the connector's
  `auth` module (`scopes.py:GMAIL_DWD_TRIAGE_SCOPES`, WRITTEN). Today this
  variant documents the target posture of an assisted install; it is not
  something the shipped code can request. **Do not sell it as available until
  that flow runs.**

**STANDARD (interactive) — code-layer, what actually runs today:**

```
https://www.googleapis.com/auth/gmail.modify
```

Covers list, get, label modify, and `drafts.create` in one scope, so the consent
screen shows a single permission. It is **also accepted by Gmail's send
methods.** The no-send guarantee in this configuration is a property of rTech's
codebase — no send code path exists, and two scanners prove it every build — not
a property of Google's servers. Say the words "code-layer" to the client.

**IMPOSSIBLE under either STANDARD variant:** filter/rule management (that is
`gmail.settings.basic`), permanent deletion (that is `mail.google.com`), and
mailbox settings changes.

### 3.3 FULL

```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.settings.basic
```

`gmail.modify` already carries send and compose, so **no separate `gmail.send` is
needed** — and adding one would be pure surface with no capability gain.
`gmail.settings.basic` — *"See, edit, create, or change your email settings and
filters in Gmail"* — is the documented scope for `users.settings.filters.create`.
([users.settings.filters.create][g-filters])

**Still IMPOSSIBLE:** permanent deletion. That requires `https://mail.google.com/`
(*"Read, compose, send, and permanently delete all your email"*), which **no
posture in this kit ever grants**. Gmail's `trash`/`untrash` under `gmail.modify`
is recoverable; irreversible deletion is not reachable.

**Forwarding on Gmail is an asymmetry in the other direction.** Graph has a
first-class `/forward` endpoint gated by `Mail.Send`. Gmail has no separate
forward permission — a forward is just a `messages.send` call with quoted
content, so **it is not separately grantable or separately deniable.** On Gmail,
"can send" and "can forward" are the same grant. You cannot allow one and refuse
the other at the scope layer. **Graph can express that distinction; Gmail
cannot.**

### 3.4 Gmail scope summary

| Capability | Scope | Class | Send-capable? |
| --- | --- | --- | --- |
| Read messages | `gmail.readonly` | restricted | No |
| Label / file (DWD only) | `gmail.modify.restricted` | restricted | **No** |
| Read + file + draft + send | `gmail.modify` | restricted | **Yes** |
| Drafts | `gmail.compose` | restricted | **Yes** |
| Send only | `gmail.send` | sensitive | Yes |
| Filters / settings | `gmail.settings.basic` | restricted | No |
| Everything incl. permanent delete | `https://mail.google.com/` | restricted | Yes — never granted |

Note the class column: most of these are **restricted scopes**, subject to
Google's restricted-scope OAuth verification and a third-party security
assessment where data is stored or transmitted server-side. Two facts make this
tractable and both should be said: the app is **internal to the client's own
tenant**, and the connector **stores nothing server-side and transmits nothing to
rTech**. If the client's security team wants the assessment scope defined, that
is a question for their own Google account team, not for this paragraph.

---

## 4. Verifying a posture after granting it

Three checks. Run all three; the third is the one that matters.

### 4.1 In your own console

**Entra:** Enterprise applications → the app → **Permissions**. The
admin-consented delegated permission list must match the posture in §2 exactly —
same names, nothing extra. Cross-check against **App registrations → the app →
API permissions**; a permission can be *requested* on the registration and not
*consented*, and only the consented list is real.

**Google Admin:** Security → Access and data control → API controls →
**Domain-wide delegation** → the service account's Client ID. The scope list is
literally what you typed. For interactive installs, check the user's own
**Third-party apps & services** grant instead.

### 4.2 What the connector reports

`uv run inbox-zero-connector show-scopes` prints the granted set read back from
the live token and the capability list derived from it, plus the enforcement
layer (`scope-layer` or `code-layer`) for each provider.

**Run it in front of the client's technical stakeholder and read it aloud.** It
takes thirty seconds and converts a brochure claim into something they watched.

**A mismatch between the connector's report and your console is a stop.** Do not
reconcile it afterwards. Either the wrong app was consented, the token predates
the grant change, or the discovery layer has a bug — all three are worth halting
for.

*(PROPOSED: `show-scopes` currently reports the posture computed from local
config, not from a live token read-back. The read-back is what `grants.py` adds.
Until that lands, this check proves what the connector intends to request, not
what the tenant granted.)*

### 4.3 The negative check — proving a capability is absent, not merely unused

This is the check a security-conscious buyer actually wants, and it is the one
usually skipped. "The tool isn't in the list" proves the connector did not
register it. It does not prove the token would be refused.

**Prove it at the provider, with the client's own token, out of band:**

**Graph, proving `Mail.Send` is absent** — from the client's machine, with the
connector's token:

```
POST https://graph.microsoft.com/v1.0/me/sendMail
```

Expect **`403 Forbidden`** with error code `ErrorAccessDenied` and a message
naming the missing scope. A `401` means an expired token, not a denied
permission — refresh and repeat. Record the response body.

**Graph, proving rules management is absent:**

```
GET https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messageRules
```

Expect `403` under READ-ONLY and STANDARD; expect `200` under FULL.

**Gmail, proving send is refused (DWD STANDARD only):** a `users.messages.send`
call with a `gmail.readonly` + `gmail.modify.restricted` token is rejected
server-side. Under **interactive** Gmail, this check **will not fail** — the
token genuinely can send, and that is exactly why that posture is labelled
code-layer. **Do not run this check on interactive Gmail and report a pass; there
is no pass to report.** The controls there are the code scanners (§4.4) and the
tenant-side app access control (§5.4).

**Decode the token to see the granted set directly.** A Graph access token is a
JWT; its `scp` claim lists the delegated scopes as a space-separated string.
Decoding it — offline, never pasted into a web decoder — shows you the ceiling
without asking the connector anything. **A token is a credential: decode it
locally, and do not paste it into a browser tool, a ticket, or a chat.**

### 4.4 The artifacts a client's own reviewer can run

Hand these three paths over. They need no rTech involvement:

- `connector/src/inbox_zero_connector/scopes.py` — the security manifest; every
  scope constant carries its vendor citation.
- `connector/tests/test_security_surface.py` — fails the build if a send-capable
  scope string or a send/delete endpoint literal appears in executable source.
- `_graph/done-means/inbox-zero-no-send.sh` — a standalone lexical scan with no
  Python dependency, runnable on the tree they were given.

---

## 5. Changing a posture on a live install

### 5.1 Tightening (removing a capability)

**Graph:** Enterprise applications → the app → Permissions → **Revoke** the
permission, or remove it from the registration and re-consent. Revocation applies
at the directory; existing access tokens remain valid until they expire.

**Gmail DWD:** edit the scope list on the domain-wide delegation entry. Gmail
interactive: the user revokes at their Google Account → Third-party apps, or you
remove the app's trust in App access control.

**Then force a re-auth.** `uv run inbox-zero-connector logout <provider>` clears
the cached token from the OS keychain, and the next `auth` mints a token against
the new grant. **Skipping this leaves the old, wider token in play until it
expires** — which is the entire failure mode of "we tightened it" claims.

### 5.2 Loosening (adding a capability)

Add the permission, grant admin consent, then **`logout` and re-`auth`**. An
existing token does not gain scopes it was not minted with; the connector will
keep reporting the old capability set until it holds a new token. This is not a
bug and it is worth telling the client so nobody spends an afternoon on it.

**What the user sees:** on Graph with admin consent already granted, the device
code flow completes with no additional prompt. Without admin consent, the user
sees a consent screen listing the new permission. On Gmail interactive, the user
sees a Google consent screen naming each scope. On Gmail DWD there is no user
prompt at all — the admin grant is the whole authorization, which is convenient
and is also worth stating to a firm that expects to see a prompt.

### 5.3 Revocation mid-flight — what actually happens

**UNVERIFIED.** The connector's behaviour when a grant is revoked during a
running sweep has not been observed. The design intent is that the next API call
returns `403`, the connector treats it as a hard failure, logs it, and stops
rather than retrying or degrading — but **nothing has been run against a real
revocation.**

**What would settle it**, and this is a genuinely useful hour on a test tenant:
start a sweep against a mailbox with several hundred messages, revoke
`Mail.ReadWrite` in Entra mid-run, and record (a) whether the in-flight token
keeps working until expiry — it very likely does, since Entra access tokens are
bearer tokens valid roughly an hour and revocation is not instantaneous — (b) the
exact HTTP status and error code on the first call after the token refreshes, and
(c) whether the connector halts loudly or silently skips. Do this before telling
any client that revocation is an emergency stop. **It probably is not one**; the
emergency stop is `logout` plus revocation together.

### 5.4 The tenant-side control that survives our code being wrong

Scope choice is defense in depth. The control that holds regardless is
tenant-side, and on a FULL or Gmail-interactive install it is not optional:

- **Entra:** Enterprise applications → Consent and permissions → **User consent
  settings** — do not allow user consent for apps, or permit it only for
  low-impact permissions. `Mail.Send` is not low-impact. Where the client
  operates **app consent policies**, ensure none permits `Mail.Send` for this
  app.
- **Exchange Online `ApplicationAccessPolicy`:** scope the app's reach to a
  mail-enabled security group containing only the in-scope mailboxes. This turns
  "the token cannot send" into "the app cannot reach mailboxes it was not granted."
- **Google Admin:** API controls → **App access control** — mark the app
  **Trusted** for exactly the agreed scopes, and set unconfigured third-party
  apps to **Blocked** or **Limited**. On a Gmail interactive install this is what
  puts a wall back into a picture where the scope layer gave one up. **Do not
  skip it there.**

Record what was applied, where, by whom, and on what date, in the client's
install notes. "Declined" is a valid recorded answer; unrecorded is not.

---

## 6. Risk notes, written for a law firm

### 6.1 READ-ONLY

Residual risk is **confidentiality of the read itself**. The connector reads
privileged client communications — that is the job — and forms judgments about
them. Nothing is written to the mailbox and nothing is transmitted to rTech;
classifications land in a local ledger on the client's own machine. The
questions worth pre-answering for a firm's ethics counsel: where the AI runtime
processes message content, whether that runtime's provider retains it, and
whether the engagement covers a matter with a heightened confidentiality order.
**Those are questions about their AI vendor, not about this connector**, and the
connector's read-only posture does not answer them.

No risk of mail movement, alteration, or exfiltration. For most firms this is the
posture to start in and stay in for the first engagement.

### 6.2 STANDARD

Two real risks, neither catastrophic:

- **Misfiling.** A message filed into the wrong folder is a message the attorney
  does not see when they expect to. Mitigation is the closed taxonomy, the
  ledger, and undo — every filing is recorded and reversible, and the first
  supervised sweep is run on the firm's own mail with the operator watching.
- **Draft content leaking into a send by human error.** The connector cannot
  send, but a person can. A draft that misstates a matter fact and gets sent by a
  hurried associate is a real professional-responsibility exposure. The kit's
  missing-fact refusal exists for this: it stops and asks rather than inventing a
  number. Tell the firm the drafts are a starting point requiring attorney
  review, because that is both true and the standard they are already held to.

`Mail.ReadWrite` is whole-mailbox. If the firm has matters requiring hard
segregation, the answer is a separate mailbox, not a permission — Graph offers no
folder-scoped alternative.

### 6.3 FULL — read this before granting it to a law firm

**Forwarding is the highest exfiltration risk in the entire capability set, and
it is not close.** A forward moves privileged client mail *out of the mailbox* to
an arbitrary external address in a single API call. Filing is reversible;
drafting is inert; a forward is neither. Once it is delivered, it is gone —
outside the firm's custody, outside their retention policy, and outside any undo
this connector could offer. For a firm with a duty of confidentiality to every
client on that mail server, an automated forward capability is a categorically
different thing from an automated filing capability, and it should be presented
that way rather than as one more item on a feature list.

On **Graph** you can grant send while withholding forward only by withholding
`Mail.Send` entirely — the two share the permission. On **Gmail** you cannot
separate them at all (§3.3). Either way, **granting FULL grants forwarding.**
There is no posture in this kit that gives send-without-forward.

**Rules management carries two specific, documented hazards:**

1. **Exchange PowerShell rule writes can destroy the client's existing
   client-side rules.** Microsoft Learn states on the `New-InboxRule` page that
   creating or modifying a rule via Exchange PowerShell **removes client-side
   rules disabled by Outlook, and outbound rules** (DOCUMENTED — kit research,
   `CAPABILITIES.md` §3.1). A firm with fifteen years of hand-built rules, or an
   MSP who built them, can lose that silently. The kit's own standing rule is
   **never programmatically write inbox rules into a client mailbox**, and the
   research calls destroying a partner's rules an MSP-relationship-ending event.
   If you grant `MailboxSettings.ReadWrite`, grant it knowing the connector's
   design position is not to use it that way.
2. **The rules quota is 256 KB total per mailbox, shared with everything the
   client already has.** Valid range is 32 KB–256 KB, and in Exchange Online it
   **cannot be increased** past 256 KB — only decreased (DOCUMENTED). Consuming
   it with generated sender lists breaks the firm's ability to write their own
   rules, with a confusing error and no obvious cause. **Do not cite "45 rules"
   or "100 rules" as a Microsoft limit** — Microsoft states there is no maximum
   *count*; the limit is bytes, and it applies only to enabled rules. The
   circulating count figures are derived estimates (UNVERIFIED), and citing a
   wrong number to an IT-literate reviewer costs credibility on a checkable fact.

**The composition worth proposing instead:** the connector writes **categories**,
the client's own rules act on them. `AnyCategory` and `AssignedCategories` are
real `New-InboxRule` conditions, so the firm can write rules against our
classification using their mechanism. We supply judgment; their rules supply
placement. Nothing of theirs is overwritten and the quota is untouched.

---

## 7. Decision table

| Client type | Posture | Why |
| --- | --- | --- |
| **Law firm, any size** | **READ-ONLY** for the first engagement | Privileged mail. Prove the classification is right before it touches anything. Nothing leaves, nothing changes, and the ethics conversation stays short. |
| **Law firm, after a clean supervised run** | **STANDARD** | Filing and drafts with an attorney sending. On Graph this is scope-layer — Microsoft refuses send, not us. This is where most firms should land permanently. |
| **Law firm** | **Never FULL** | Forwarding moves privileged mail out of the firm's custody irreversibly (§6.3). If a firm asks for it, that is a conversation about who is accountable, not a config change. |
| **Medical, financial, or any regulated SMB** | **READ-ONLY → STANDARD** | Same reasoning. Regulated data plus irreversible egress is not a trade worth the time saved. |
| **Agency, marketing, sales-led SMB** | **FULL** | Outbound volume is the actual job, forwarding is routine, and an errant forward is an embarrassment rather than a breach. This is what FULL is for. |
| **Any client, Gmail, wanting drafts** | **STANDARD (interactive), stated as code-layer** | Gmail sells no draft-without-send scope. Say "code-layer" out loud and apply the App access control in §5.4. Do not let it be heard as the Microsoft guarantee. |
| **Any client whose security team asks first** | **READ-ONLY**, then widen on evidence | The negative check in §4.3 is a better sales artifact than any capability list. Widening after a clean receipt is an easy conversation; narrowing after an incident is not. |

**The opinionated version, in one line:** default to READ-ONLY, earn STANDARD,
and treat FULL as a posture for clients whose mail is not privileged.

---

## References

All fetched 2026-08-18 from vendor documentation, quoted from each page's own
permissions/scopes table rather than from summary prose.

[ms-list]: https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0
[ms-move]: https://learn.microsoft.com/en-us/graph/api/message-move?view=graph-rest-1.0
[ms-createreply]: https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0
[ms-reply]: https://learn.microsoft.com/en-us/graph/api/message-reply?view=graph-rest-1.0
[ms-forward]: https://learn.microsoft.com/en-us/graph/api/message-forward?view=graph-rest-1.0
[ms-rules]: https://learn.microsoft.com/en-us/graph/api/mailfolder-post-messagerules?view=graph-rest-1.0
[ms-permref]: https://learn.microsoft.com/en-us/graph/permissions-reference
[g-scopes]: https://developers.google.com/workspace/gmail/api/auth/scopes
[g-modify]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify
[g-filters]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters/create

- [List messages][ms-list] — `Mail.ReadBasic` least privileged; `Mail.Read`,
  `Mail.ReadWrite` higher.
- [message: move][ms-move] — `Mail.ReadWrite`, no higher available.
- [message: createReply][ms-createreply] — `Mail.ReadWrite`, no higher available;
  returns `isDraft: true`.
- [message: reply][ms-reply] — `Mail.Send`, no higher available.
- [message: forward][ms-forward] — `Mail.Send`, no higher available.
- [Create rule][ms-rules] — `MailboxSettings.ReadWrite`, no higher available.
- [Graph permissions reference][ms-permref] — `Mail.ReadWrite` description
  including "Does not include permission to send mail."
- [Gmail API scopes][g-scopes] — every Gmail scope string and its
  restricted/sensitive classification.
- [users.messages.modify][g-modify] — accepts `mail.google.com`, `gmail.modify`,
  `gmail.modify.restricted`; documents the DWD requirement for the last.
- [users.settings.filters.create][g-filters] — `gmail.settings.basic`.

**Marked UNVERIFIED in this document:** connector behaviour on mid-flight grant
revocation (§5.3); Exchange Online Archive mailbox exclusion (§2.3, inherited
from `CAPABILITIES.md` §2.1); circulating inbox-rule count limits (§6.3, and the
point is that they should not be cited at all).
