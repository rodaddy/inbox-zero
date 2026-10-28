# Inbox Zero — Google Workspace Admin Runbook

**Audience: the Google Workspace super administrator**, or the operator sitting
next to one. You have an Admin console, you own the domain, and you are deciding
what this connector is allowed to do inside it. This runbook is exact because a
wrong console path or a wrong scope string in a runbook gets pasted into a
production domain.

**Version:** 0.1.0 · **Status: WRITTEN.** No step in this runbook has been
executed against a live Workspace domain in this session. Every Admin console
path, app-access state name, and Gmail scope string below is cited to Google's
own documentation, fetched **2026-08-18**. Documentation-verified is not
run-verified: treat each path as correct-as-documented and confirm it on screen
before you act on it. Items Google's own docs do not state, or state
inconsistently, are marked **UNVERIFIED** or **CONTESTED** and are never
guessed.

**This is the fourth buyer.** The kit sells across four distinct buying
situations and this document owns exactly one of them:

| # | Buyer | Runbook |
| --- | --- | --- |
| 1 | Microsoft 365 tenant with an MSP | `POSTURE-RUNBOOK.md` |
| 2 | Microsoft 365 self-managed owner-admin | `POSTURE-RUNBOOK.md` |
| 3 | **Google Workspace with an admin console** | **this file** |
| 4 | Consumer `@gmail.com`, no admin at all | `SELF-SERVE-CONSENT.md`, and §6 here |

Scope vocabulary (`gmail.readonly`, `gmail.modify`, `gmail.modify.restricted`,
`gmail.compose`) and the three posture names are shared with
`POSTURE-RUNBOOK.md` §3 and defined in
`connector/src/inbox_zero_connector/scopes.py`. This document does not restate
the scope analysis; it owns the **admin console layer** — what a Workspace admin
can enforce that a consumer Gmail user cannot, and how to enforce it.

---

## 1. What a Workspace admin controls that a consumer Gmail user cannot

This is the whole reason buyer 3 is a different sale from buyer 4. A consumer
Gmail user has exactly one control: their own consent screen, and their own
ability to withdraw it afterwards. A Workspace admin has four controls that sit
*above* the user, and the user cannot override any of them.

### 1.1 App access control — Trusted / Limited / Blocked / Specific Google data

**Admin console path:** Menu → **Security** → **Access and data control** →
**API controls** → **Manage Third-Party App Access**.
([Control which third-party & internal apps access Google Workspace data][g-3p])

Each app is set to one of these states, quoted from Google's own documentation:

| State | Google's definition |
| --- | --- |
| **Trusted** | "App has access to all Google Workspace services (OAuth scopes), including restricted services." |
| **Limited** | "App can only access unrestricted services." |
| **Specific Google data** | "Can request data access only to scopes that you specify when configuring the app." |
| **Blocked** | "App can't access any Google data." |

([Control which apps access Google Workspace data][g-appaccess])

To change one: click the app, select **Change access**, choose the level, then
**Change**. ([Control which third-party & internal apps access Google Workspace
data][g-3p])

**A consumer Gmail user has no equivalent of this at all.** There is no state
between "I consented" and "I did not."

### 1.2 Restricting third-party access by scope

Google separates two different restrictions and the distinction matters when you
are writing a security memo:

- **Service-level.** Under **Manage Google Services**, an admin marks a service
  (Gmail, Drive, and others) restricted or unrestricted. When a service is
  restricted, only apps set to **Trusted** or **Specific Google data** may reach
  it.
- **Scope-level.** For Gmail and Drive specifically, Google exposes a set of
  **high-risk OAuth scopes** that can be restricted, with a setting to "allow
  users to give access to OAuth scopes that aren't classified as high-risk."
  Google's high-risk Gmail list includes `gmail.send`.
  ([Control which apps access Google Workspace data][g-appaccess])

**One honest limit, stated because a security reviewer will find it:**
Google's documentation describes scope restriction as operating over
**predefined high-risk scope groups per service**, not over arbitrary individual
scopes. An admin cannot generally hand-write "deny exactly this one scope string
and nothing else." The **Specific Google data** state is the mechanism that
comes closest, because it constrains an app to a scope list the admin configures.
([Control which third-party & internal apps access Google Workspace data][g-3p])

### 1.3 The "Trust internal apps" control — and why it matters to this kit

Because this kit's design is a **per-client OAuth client in the client's own
Google Cloud project** (`INSTALL-RUNBOOK.md` §9: never a shared rTech client ID),
the connector is an **internal app** owned by the client's own organization —
not a third-party app.

Google treats those differently. Its documentation states that for internally
built apps owned by your organization, you can trust all such apps to access
restricted Google Workspace APIs via a single **Trust internal apps** checkbox,
rather than trusting each one individually.
([Control which third-party & internal apps access Google Workspace data][g-3p])

**Read that as a warning, not a convenience.** If the client's domain has
**Trust internal apps** ticked, then trusting this connector is not a decision
anyone made about this connector — every internal OAuth client in the
organization inherits the same trust. On a law-firm install, prefer configuring
this specific app explicitly (§4.2) over relying on a blanket internal-app trust,
and *check which one is in force* before claiming the app is scoped.

### 1.4 Marketplace app allowlisting

**Admin console path:** Menu → **Apps** → **Google Workspace Marketplace apps**
→ **Apps list**. Under **Manage access to apps**, an admin chooses whether users
may install any apps, only allowlisted apps, or no apps. Apps are added with
**Allowlist app**. Managing this requires the Google Workspace Marketplace
administrator privilege.
([Manage the Marketplace app allowlist][g-mkt], [Set whether users can install
Marketplace apps][g-mktinstall])

**This control does not apply to this kit and you should say so rather than
citing it as protection.** The Inbox Zero connector is a local MCP server
authenticating against the client's own OAuth client. It is not distributed
through the Google Workspace Marketplace, so it is neither allowlisted nor
blocked by that setting. The allowlist is listed here because an admin will ask
about it, and the correct answer is "different mechanism, does not govern this."
The controls that *do* govern this app are §1.1, §1.2, and §1.5.

### 1.5 Domain-wide delegation

**Admin console path:** Menu → **Security** → **Access and data control** →
**API controls** → **Manage Domain Wide Delegation**.
([Control API access with domain-wide delegation][g-dwd], [Create
credentials][g-creds])

This is the control that has no consumer analogue whatsoever: it lets a service
account act as users **without individual user consent**.
([Create credentials][g-creds]) It is powerful enough that §3 is devoted to
arguing against defaulting to it.

Requires super administrator privileges. If Multi-party approval is enabled,
both authorizing and editing or deleting a grant require approval from another
super admin. ([Control API access with domain-wide delegation][g-dwd])

---

## 2. The three postures, translated to Workspace

Posture names are shared with `POSTURE-RUNBOOK.md` so the kit speaks one
vocabulary across both platforms. The scope strings below are quoted from
Google's own references; the **negative** lists are what a security-conscious
buyer actually reads.

Scope classification, from Google's Gmail API scopes page: `gmail.readonly`,
`gmail.modify`, and `https://mail.google.com/` are **restricted**;
`gmail.compose`, `gmail.send`, and `gmail.settings.basic` are listed under
**sensitive**. ([Gmail API scopes][g-scopes])

> **Correction against a sibling document.** `POSTURE-RUNBOOK.md` §3.4 lists
> `gmail.compose` and `gmail.settings.basic` as *restricted*. Google's scopes
> page classifies both as **sensitive**. The distinction changes which
> verification track applies (§5.4), so it is called out rather than smoothed
> over. Verified 2026-08-18 against [the scopes page][g-scopes].

### 2.1 READ-ONLY

```
https://www.googleapis.com/auth/gmail.readonly
```

Google's description: *"View your email messages and settings."* Classified
**restricted**. ([Gmail API scopes][g-scopes])

**Connector can:** read messages and threads, classify them, produce the daily
digest, sample sent mail for voice. Tools: `list_inbox`, `survey_inbox`,
`get_thread`, `read_sent_sample`, `get_daily_digest`.

**IMPOSSIBLE under this posture:**

- **Cannot add or remove a label** — cannot file, archive, star, or mark read.
  Label modification requires `gmail.modify` or `gmail.modify.restricted`;
  neither is present. ([users.messages.modify][g-modify])
- **Cannot create a draft.** `users.drafts.create` does not accept this scope.
- **Cannot send, reply, or forward.** `users.messages.send` accepts only
  `https://mail.google.com/`, `gmail.modify`, `gmail.compose`, and `gmail.send`
  — this scope is on none of those lists. ([users.messages.send][g-send])
- **Cannot create or change a filter.** That is `gmail.settings.basic`.
  ([Gmail API scopes][g-scopes])
- **Cannot delete anything, recoverably or permanently.**

Nothing in the mailbox changes. The connector reads, forms a judgment, and
writes that judgment only into its own local ledger.

### 2.2 STANDARD — and Workspace is the only place the strong version exists

Gmail has **two** STANDARD variants and they are not equivalent. Which one you
get depends on the installation shape in §3, and this is the single most
important technical fact on the Google side of this kit.

**STANDARD (DWD) — scope-layer, the strong one:**

```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify.restricted
```

`gmail.modify.restricted` is the only Gmail mutation scope not accepted by any
send method. `users.messages.send` accepts exactly `https://mail.google.com/`,
`gmail.modify`, `gmail.compose`, and `gmail.send`; this scope is on none of them.
([users.messages.send][g-send], [users.messages.modify][g-modify])

**Two hard constraints, both verified at primary source:**

- **It is DWD-only.** Google's `users.messages.modify` reference states it
  verbatim: *"For administrators modifying message for users in their
  organization, requests require authorization with a service account that has
  domain-wide delegation authority to impersonate users with the
  `https://www.googleapis.com/auth/gmail.modify.restricted` scope."*
  ([users.messages.modify][g-modify]) It does not appear on the public Gmail API
  scopes list at all ([Gmail API scopes][g-scopes]). **PROVEN LIVE 2026-08-18**
  (recorded in `scopes.py`): an interactive consent request including this scope
  is refused with `Error 400: invalid_scope` before sign-in completes.
- **The DWD service-account flow is NOT YET IMPLEMENTED** in the connector's
  `auth` module (`scopes.py:GMAIL_DWD_TRIAGE_SCOPES`, WRITTEN). This variant
  documents the target posture of an assisted install. **Do not sell it as
  available until that flow runs.**

**STANDARD (interactive) — code-layer, what actually runs today:**

```
https://www.googleapis.com/auth/gmail.modify
```

Google's description: *"Read, compose, and send emails from your Gmail account.
This scope does not allow immediate, permanent deletion of threads and messages,
bypassing the trash."* ([Gmail API scopes][g-scopes]) It **is** accepted by
`users.messages.send`. ([users.messages.send][g-send])

The no-send guarantee here is a property of rTech's codebase — no send code path
exists, and two scanners prove it every build — **not** a property of Google's
servers. Say the words "code-layer" to the client.

**IMPOSSIBLE under either STANDARD variant:**

- **Cannot permanently delete mail.** That requires `https://mail.google.com/`,
  which no posture in this kit ever grants. ([Gmail API scopes][g-scopes])
- **Cannot create or modify a filter or forwarding rule.** That is
  `gmail.settings.basic`, absent here.
- **Cannot reach another user's mailbox** under interactive consent — the token
  is the signed-in user's own.

**Additionally IMPOSSIBLE under STANDARD (DWD) specifically, and this is the
selling point:** cannot create a draft and **cannot send**, refused by Google's
servers rather than by our code. Under STANDARD (interactive), drafting works
and sending is refused only by our code.

### 2.3 FULL

```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.settings.basic
```

`gmail.modify` already carries compose and send, so **no separate `gmail.send`
is needed** and adding it would be pure attack surface with no capability gain.
`gmail.settings.basic` — *"See, edit, create, or change your email settings and
filters in Gmail"* — is the filters scope. ([Gmail API scopes][g-scopes])

**Still IMPOSSIBLE, even here:**

- **Cannot permanently delete mail.** `https://mail.google.com/` is never
  granted under any posture in this kit. Trash and untrash under `gmail.modify`
  are recoverable; irreversible deletion is not reachable.
  ([Gmail API scopes][g-scopes])
- **Cannot change domain or tenant configuration.** `gmail.settings.basic` is
  the signed-in (or impersonated) user's own Gmail settings only.
- **Cannot reach mailboxes outside the grant** — under interactive, the one
  signed-in user; under DWD, whatever impersonation the admin permitted (§3.2).

Read §3.4 before granting FULL to a law firm.

---

## 3. Two installation shapes, and when each is right

This is the decision a Workspace admin actually has to make, and it is not the
same decision the Microsoft buyer makes.

### 3.1 Shape A — per-user OAuth, admin controls the app allowlist

Each user runs the connector's interactive `auth` and consents for their own
mailbox. The admin does not hand out mailbox access; the admin decides **whether
this app is permitted to ask at all**, using §1.1 and §1.2.

- **Scopes reachable:** READ-ONLY, STANDARD (interactive), FULL. **Not**
  STANDARD (DWD) — `gmail.modify.restricted` is refused at the consent screen.
- **Blast radius:** one mailbox per consent. A compromised token is one user's
  mail.
- **Consent is visible.** The user saw a screen and agreed. There is a human
  record of who allowed what.
- **Revocation is two-sided.** The admin can block the app domain-wide (§4.3);
  the user can withdraw their own grant.
- **Cost:** the strong scope-layer Gmail posture is unavailable. Best Gmail
  guarantee under this shape is **code-layer**.
- **Status:** this is the shape the shipped connector actually implements today.

### 3.2 Shape B — domain-wide delegation with a service account

A service account in the client's Google Cloud project is granted named scopes
in **Manage Domain Wide Delegation** and impersonates users **without individual
user consent**. ([Create credentials][g-creds])

- **Scopes reachable:** all of the above **plus** STANDARD (DWD), the only Gmail
  posture where the no-send guarantee is Google's rather than ours.
- **Blast radius: the entire domain.** The grant is per-scope, not per-user. A
  DWD grant for a Gmail scope is a grant over **every mailbox in the domain**,
  including partners' and the super admin's, unless separately constrained.
- **No user consent and no user-visible record.** That is the feature, and it is
  also the audit problem: impersonated access is non-repudiation-hostile.
- **Key material exists.** A service account JSON key is a bearer credential
  that does not expire on its own.
- **Status:** **NOT YET IMPLEMENTED** in the connector's `auth` module. Today
  this shape is a design target, not a deployable option.

### 3.3 The honest trade-off, in Google's own words

This kit does not need to editorialize here, because Google already did. From
Google's domain-wide delegation best-practices page, quoted verbatim:

> "Avoid using domain-wide delegation if you can accomplish your task directly
> using a service account or by using OAuth consent."

> "Use domain-wide delegation only when you have a critical business case that
> requires an app to bypass user consent to access Google Workspace data."

> "Try alternatives such as OAuth with user consent or use Marketplace apps."

> "Ensure that service accounts with domain-wide delegation have only the
> essential privileges needed to perform their intended functions."

> "Avoid using service account keys. Using service account keys is not necessary
> to perform domain-wide delegation. Use the signJwt API instead."

> "Minimize the number of people that have edit access to Google Cloud projects
> with domain-wide delegation set up."

Google names the risks as privilege escalation, non-repudiation, service account
impersonation through keys, lateral movement, and insider risk from excessive
privileges. ([Domain-wide delegation best practices][g-dwdbest])

**Say it plainly to the client:** DWD is a standing keys-to-the-kingdom grant
across every mailbox in the domain. It is not a configuration detail and it is
not the default. It gets argued for, in writing, or it does not happen.

> **Note the tension inside this kit, and do not hide it.** DWD is simultaneously
> the *only* path to Gmail's strongest scope-layer posture (§2.2) and the
> *broadest* grant available in Workspace. Buying the stronger no-send guarantee
> means accepting a wider impersonation surface. That is a genuine trade, not a
> free upgrade, and a buyer who is told only the first half has been sold badly.

### 3.4 Recommendation for a law firm

**Recommend Shape A — per-user OAuth — and specifically READ-ONLY first, then
STANDARD (interactive) once the firm trusts the classifications.** Do not default
to DWD.

The argument, in the order it should be made to a firm:

1. **Consent is evidence, and law firms run on evidence.** Under Shape A every
   mailbox the software touched has a human who agreed to it. Under DWD there is
   a service account that could reach every mailbox and no per-mailbox record of
   agreement. When the firm's own client asks who could read the matter file,
   "each attorney authorized it individually" is a better answer than "one key
   could read all of it."
2. **Confidentiality is per-matter, and DWD is per-domain.** A firm's ethical
   walls are between people. A domain-wide grant is structurally the wrong shape
   for an organization whose defining control is that some people must not see
   some mail.
3. **Blast radius on compromise.** The operator's laptop holding one attorney's
   token is a bad day. The same laptop holding a DWD service-account key is
   every mailbox in the firm, including the managing partner's.
4. **Google recommends against it.** Point 3.3 at the firm's IT counsel. "The
   vendor said no" is weaker than "Google says avoid this unless you have a
   critical business case," and the firm's own security reviewer will find that
   page.
5. **The thing DWD buys is not worth it here yet.** DWD's unique prize is the
   scope-layer no-send guarantee — and that flow **is not implemented**, so
   today DWD buys the firm nothing the interactive flow does not already do,
   while costing a domain-wide grant.

**When to reverse the recommendation:** if and only if the DWD flow ships, the
firm's security reviewer specifically demands that no-send be enforced by
Google's servers rather than by rTech's code, and the firm accepts the
impersonation surface in writing. In that case constrain it hard — the two DWD
triage scopes only, no `gmail.compose`, impersonation limited per §4.4, key
handling per §3.3 — and record the decision under `INSTALL-RUNBOOK.md` §4.3.

**Never default to DWD because it is easier to install.** Installation
convenience is not a security argument, and on this kit it is the exact
rationalization the runbook exists to block.

---

## 4. How the admin verifies and enforces a posture

Do these in order. §4.5 is the one that matters, because it is the only one that
tests a negative.

### 4.1 See what the app is actually granted

**Menu → Security → Access and data control → API controls →
Manage Third-Party App Access.** ([Control which third-party & internal apps
access Google Workspace data][g-3p])

The **Accessed apps** view shows, per app, the number of users accessing it and
the Google service APIs (OAuth2 scopes) that app is using. App information can be
downloaded as a **CSV** including verification status, number of users,
organizational unit, requested services, and API scopes.
([Control which apps access Google Workspace data][g-appaccess])

**Timing caveat, and it will bite you during a live install:** the accessed-apps
list updates **48 hours after a token is granted or revoked**.
([Control which third-party & internal apps access Google Workspace data][g-3p])
An app you authorized ten minutes ago may not appear yet. **Absence from this
list is not proof of absence of a grant.** Do not run this check as your live
verification during the install session and do not let a client conclude
anything from an empty row.

For a DWD grant, use **Manage Domain Wide Delegation** instead — it is
immediate. Point to a client ID and select **View details** to see the full
client name and the list of scopes. ([Control API access with domain-wide
delegation][g-dwd])

### 4.2 Restrict the app to exactly the agreed scopes

1. In **Manage Third-Party App Access**, click the app.
2. **Change access.**
3. Choose **Specific Google data** and configure exactly the scope list agreed
   for the chosen posture in §2 — this is the state whose definition is "Can
   request data access only to scopes that you specify when configuring the app."
   ([Control which apps access Google Workspace data][g-appaccess])
4. **Change.**

Where the firm wants a harder floor under this, also restrict Gmail as a service
under **Manage Google Services**, so that only Trusted or Specific-Google-data
apps can reach Gmail at all. ([Control which apps access Google Workspace
data][g-appaccess])

**Check §1.3 before you claim this worked.** If **Trust internal apps** is
enabled, an internal app may be trusted by that blanket setting regardless of
what you configured here.

### 4.3 Revoke

- **Block the app domain-wide:** **Manage Third-Party App Access** → the app →
  **Change access** → **Blocked** ("App can't access any Google data").
  ([Control which apps access Google Workspace data][g-appaccess])
- **Revoke a DWD grant:** **Manage Domain Wide Delegation** → click the client
  name → **Delete**. Google's documentation is blunt about the consequence:
  applications depending on that authorization "will immediately stop working."
  ([Control API access with domain-wide delegation][g-dwd])
- **Token revocation on restriction:** when access is changed to Restricted,
  "any previously installed apps that you haven't trusted stop working and
  tokens are revoked." ([Control which third-party & internal apps access Google
  Workspace data][g-3p])
- **Password change:** Google separately documents automatic OAuth 2.0 token
  revocation upon password change. ([Automatic OAuth 2.0 token revocation upon
  password change][g-tokrev])
- **Locally, on the machine:** `uv run inbox-zero-connector logout google`
  clears the token from the OS keychain. This is a local cleanup, **not** a
  domain-level revocation, and must never be offered as one.

**UNVERIFIED — do not paste this path into a client's console.** Google's
documentation states that the Security page "allows you to see all active
3-legged OAuth 2.0 tokens for a given user for a given application" and that
"the token listing and revocation is by user by app" ([How authorized access
works][g-authz]), but the pages fetched in this session **do not state the exact
Admin console navigation for that per-user view**. A per-user token revocation
path is therefore recorded as UNVERIFIED. Find it in the live console with the
admin, or use the app-level Blocked state in §4.3, which *is* documented. Do not
guess a menu path here.

### 4.4 Constrain DWD impersonation, if Shape B was chosen

DWD grants the service account impersonation over the domain for the granted
scopes. Google's own guidance is to grant "only the essential privileges needed"
([Domain-wide delegation best practices][g-dwdbest]).

- Configure the connector with one subject address per install
  (`INSTALL-RUNBOOK.md` §6.2) and record which mailboxes were agreed, in writing.
- Grant the narrowest scope list that serves the posture — for triage, the two
  DWD scopes and **never** `gmail.compose`.
- Prefer no service-account key at all: Google recommends the **signJwt** API
  over service account keys for DWD. ([Domain-wide delegation best
  practices][g-dwdbest])
- **Say the limit out loud:** configuring one subject address is a
  *configuration* boundary in rTech's software, not a *policy* boundary in
  Google's. The grant itself still spans the domain. If the firm wants a policy
  boundary, that is a conversation with their Google account team about
  constraining the service account's reach, and it is not something this runbook
  can promise.

### 4.5 The negative check — the one that proves anything

Everything above proves a capability is present. Prove one is **absent**:

1. With the posture applied, ask the kit to perform an action the posture
   forbids — under READ-ONLY, ask it to file a message; under STANDARD (DWD),
   ask it to draft a reply.
2. The correct outcome is a **refusal traceable to the grant**, not a silent
   no-op and not a fallback.
3. Record what you observed, including the error, and pair it with
   `uv run inbox-zero-connector show-scopes` run in front of the stakeholder.

A capability you never exercised is not a capability you proved absent.

### 4.6 What the user experiences when a scope is blocked by policy

Two different failures, and telling them apart saves a support call:

- **Blocked by admin policy:** Google's documentation states that if a user
  tries to install or sign in to an app the admin has not trusted that accesses
  a restricted service, "they're notified that the app is blocked." Admins can
  configure a custom message via the **User message** setting; otherwise a
  default message is shown. ([Control which apps access Google Workspace
  data][g-appaccess], [Control which third-party & internal apps access Google
  Workspace data][g-3p])
- **Refused as an illegal scope request:** an interactive consent request
  including `gmail.modify.restricted` fails with `Error 400: invalid_scope`
  ("Some requested scopes cannot be shown") **before sign-in completes** —
  proven live 2026-08-18, recorded in `scopes.py`. This is not an admin block
  and no console change will fix it; it means the wrong installation shape was
  chosen (§3.1).

**The exact rendered wording of the admin-block screen is UNVERIFIED** — Google
documents that a notification occurs and that it is customizable, not its default
text. Do not quote a specific error string to a client.

---

## 5. Where Gmail cannot express what Graph can, and vice versa

An honest asymmetry table beats a false parallel. A buyer comparing the two
platforms deserves the real answer, and a reviewer who finds an asymmetry you
flattened will discount everything else.

| Question | Microsoft Graph | Gmail | Who wins |
| --- | --- | --- | --- |
| "Can file and draft, cannot send" as a single grant | **Yes** — `Mail.ReadWrite`, whose own description ends "Does not include permission to send mail." | **No.** `users.drafts.create` and `users.drafts.send` accept an identical scope set. Any Gmail token that can create a draft can send it. ([drafts.create][g-draftscreate], [drafts.send][g-draftssend]) | **Graph** |
| "Can file, cannot draft, cannot send" | Not expressible as one permission — filing needs `Mail.ReadWrite`, which also drafts. | **Yes** — `gmail.modify.restricted`, but **only via DWD**. ([users.messages.modify][g-modify]) | **Gmail**, at the cost of a domain-wide grant |
| Achievable through plain user consent, no admin | Yes — `Mail.ReadWrite` is a normal delegated consent. | **No** for the strong posture. The one non-send mutation scope is refused at the consent screen. | **Graph** |
| Send and forward as separately deniable | **Yes** — `/forward` is a distinct endpoint gated by `Mail.Send`. | **No.** A forward is a `messages.send` call; there is no separate forward scope. Allow one, allow both. | **Graph** |
| Restrict the app to a named list of scopes in the admin console | Entra app registration plus consent policies | **Yes** — the **Specific Google data** state. ([Control which apps access Google Workspace data][g-appaccess]) | Comparable |
| Grant an app access without any user consent | Application (app-only) permissions with admin consent | **Yes** — domain-wide delegation. ([Create credentials][g-creds]) | Comparable, and both are dangerous |
| Restrict which mailboxes an app-level grant can reach | **Yes** — Exchange Online `ApplicationAccessPolicy` scopes the app to a mail-enabled security group. | **No documented equivalent** for constraining a DWD grant to a subset of users. Recorded **UNVERIFIED**; not found in the pages fetched this session. | **Graph**, materially |
| Permanent deletion reachable by the granted scopes | `Mail.ReadWrite` technically permits delete; the connector exposes none. | Requires `https://mail.google.com/`, never granted — irreversible delete is **out of reach at the scope layer**. | **Gmail** |
| Inbox rules / filters live in the mail permission | **No** — `MailboxSettings.ReadWrite`, a different family. | **No** — `gmail.settings.basic`, also separate. | Tie |
| Consent scope creep after install | Delegated consent is **dynamic**; the same client could request a broader token. | Same risk shape; admin app-access control is the mitigation. | Tie — mitigate both at the tenant |

**The single sentence for a buyer comparing platforms:** on Microsoft you can
have "drafts but never sends" as a guarantee from the vendor's own servers with
nothing but ordinary user consent; on Google you can have that guarantee only by
giving a service account domain-wide reach, or you accept a code-layer guarantee
instead. Neither is worse overall. They are differently shaped, and the shape
determines which install you should sell.

---

## 6. Consumer Gmail has no admin layer at all

State this plainly, because implying a control that cannot be delivered is
worse than the missing control.

**For a personal `@gmail.com` account, the user's own OAuth consent IS the
entire security boundary.** There is no Admin console, no App access control, no
Trusted/Limited/Blocked state, no domain-wide delegation, no scope restriction
policy, and no revocation-from-above. Nothing in §1 exists. Nothing in §4 can be
performed. Every control in this runbook presupposes a Workspace domain and an
administrator, and a consumer account has neither.

What a consumer user does have is exactly one control: they can review and
remove the apps connected to their own Google Account, which revokes any access
previously granted. ([Manage links between your Google Account & apps][g-consumer])

Consequences to state to the buyer without softening:

- **There is nobody above the user.** If the user consents to a broad scope,
  no policy prevents it and no administrator can retroactively narrow it.
- **The strong Gmail posture is unreachable.** STANDARD (DWD) requires a domain
  and an admin. On a consumer account the best available Gmail no-send
  guarantee is **code-layer**, held by rTech's software and provable only by
  the scanners in `INSTALL-RUNBOOK.md` §5.
- **Revocation is the user's alone**, from their own account page, and it is
  after-the-fact.

**This is a platform fact, not a product gap, and not something the kit can
engineer around.** Do not sell buyer 4 a control that only buyer 3 has. The
self-serve path and its consent-screen specifics are `SELF-SERVE-CONSENT.md`;
this section exists only to make the boundary explicit from the admin side, so
nobody reads §1 and assumes it applies to a personal mailbox.

---

## 7. Operator rules for the Workspace lane

- **Never a shared rTech client ID.** Per-client OAuth client in the client's own
  Google Cloud project (`INSTALL-RUNBOOK.md` §9). rTech stays out of the token
  chain and the client revokes without us.
- **Never default to DWD.** It is argued for in writing or it does not happen
  (§3.3). Installation convenience is not a security argument.
- **Never claim a per-user DWD boundary.** One subject address is configuration,
  not policy. The grant spans the domain (§4.4).
- **Never cite the Marketplace allowlist as protecting this kit.** Wrong
  mechanism; the connector is not a Marketplace app (§1.4).
- **Never read an empty Accessed-apps row as proof.** It lags 48 hours (§4.1).
- **Never quote a console error string you have not seen.** The block-screen
  wording is UNVERIFIED (§4.6).
- **Never say "structurally cannot send" on Gmail without naming the variant.**
  Scope-layer is DWD-only; interactive is code-layer, every time.
- **Never guess an Admin console path.** Two are marked UNVERIFIED in this
  document precisely so nobody fills them in from memory.

---

## 8. Verification checklist for a Workspace install

- [ ] Installation shape recorded — Shape A (per-user OAuth) or Shape B (DWD) —
      with the §3.4 recommendation and, if Shape B, the written argument for it.
- [ ] Posture recorded — READ-ONLY / STANDARD (interactive) / STANDARD (DWD) /
      FULL — using the same names as `POSTURE-RUNBOOK.md`.
- [ ] `uv run inbox-zero-connector show-scopes` run in front of the client's
      stakeholder; output matches §2 for the chosen posture exactly.
- [ ] Gmail no-send enforcement recorded as **scope-layer** or **code-layer**,
      matching §2.2. Interactive is always code-layer.
- [ ] App configured in **Manage Third-Party App Access**, state recorded
      (Trusted / Limited / Specific Google data / Blocked).
- [ ] **Trust internal apps** setting checked and its value recorded (§1.3) —
      because it can silently override §4.2.
- [ ] If Shape B: DWD scope list read back via **View details** and confirmed
      character-for-character; `gmail.compose` absent unless drafts were
      explicitly chosen; key handling per §3.3 recorded.
- [ ] Negative check (§4.5) executed and the refusal observed, not assumed.
- [ ] Revocation path (§4.3) demonstrated to the client's admin once, in their
      own console, before handover.
- [ ] Any UNVERIFIED item in this runbook that was resolved live is written back
      into this file with the date and what was observed.

Anything unticked is reported as unticked. A partially verified install is
reported as partially verified.

---

## Sources

All fetched 2026-08-18. Every path and quotation above traces to one of these.

[g-appaccess]: https://knowledge.workspace.google.com/admin/apps/control-which-apps-access-google-workspace-data
[g-3p]: https://knowledge.workspace.google.com/admin/apps/control-which-third-party-and-internal-apps-access-google-workspace-data
[g-dwd]: https://knowledge.workspace.google.com/admin/apps/control-api-access-with-domain-wide-delegation
[g-dwdbest]: https://knowledge.workspace.google.com/admin/apps/domain-wide-delegation-best-practices
[g-creds]: https://developers.google.com/workspace/guides/create-credentials
[g-mkt]: https://support.google.com/a/answer/6089179
[g-mktinstall]: https://support.google.com/a/answer/172931
[g-tokrev]: https://knowledge.workspace.google.com/admin/apps/automatic-oauth-20-token-revocation-upon-password-change
[g-authz]: https://knowledge.workspace.google.com/admin/apps/how-authorized-access-works
[g-scopes]: https://developers.google.com/workspace/gmail/api/auth/scopes
[g-modify]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify
[g-send]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
[g-draftscreate]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create
[g-draftssend]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/send
[g-consumer]: https://support.google.com/accounts/answer/13533235

- [Control which apps access Google Workspace data][g-appaccess] — app access
  states, scope restriction, Accessed apps view, user block notification.
- [Control which third-party & internal apps access Google Workspace data][g-3p]
  — Manage Third-Party App Access path, Change access flow, Trust internal apps,
  token revocation on restriction, 48-hour list lag.
- [Control API access with domain-wide delegation][g-dwd] — DWD console path,
  Add new / View details / Edit / Delete, multi-party approval.
- [Domain-wide delegation best practices][g-dwdbest] — every quotation in §3.3.
- [Create credentials][g-creds] — DWD path, "without requiring individual user
  consent."
- [Manage the Marketplace app allowlist][g-mkt] and [Set whether users can
  install Marketplace apps][g-mktinstall] — §1.4.
- [Automatic OAuth 2.0 token revocation upon password change][g-tokrev] — §4.3.
- [How authorized access works][g-authz] — per-user token listing exists; exact
  console path NOT stated, recorded UNVERIFIED in §4.3.
- [Gmail API scopes][g-scopes] — every scope string and description in §2, and
  the restricted-vs-sensitive classification correction.
- [users.messages.modify][g-modify] — the DWD-only sentence for
  `gmail.modify.restricted`, quoted verbatim in §2.2.
- [users.messages.send][g-send] — the four accepted send scopes.
- [users.drafts.create][g-draftscreate] / [users.drafts.send][g-draftssend] —
  the identical-scope-set fact behind §5 row 1.
- [Manage links between your Google Account & apps][g-consumer] — §6.

### Recorded UNVERIFIED / CONTESTED

- **Per-user OAuth token revocation console path** (§4.3). Google documents that
  the capability exists; the pages fetched do not give the navigation.
- **Default admin-block message text** shown to a user (§4.6). Documented as
  occurring and customizable; wording not published.
- **Constraining a DWD grant to a subset of users** as a policy control (§5).
  No documented Google equivalent of Exchange `ApplicationAccessPolicy` found.
- **CONTESTED — restricted-scope verification and security assessment for an
  internal-user-type app.** Google's "When is verification not needed" page
  states an internal-only app is not subject to the unverified-app screen or the
  100-user cap, and does not address restricted scopes; the restricted-scope
  verification page states that an app using restricted or sensitive scopes
  requires verification, and that the third-party security assessment is
  triggered by an app that "has the ability to access data from or through a
  third-party server." The two do not cleanly resolve for this kit's case. The
  connector stores nothing server-side and transmits nothing to rTech, which is
  the fact that matters for the assessment trigger. **Do not tell a client
  either way** — send the question to their own Google account team.
  Sources: https://support.google.com/cloud/answer/13464323 and
  https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
