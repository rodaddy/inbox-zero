# Inbox Zero — Self-Serve Consent (no admin, no MSP)

**Audience: the operator deciding whether this kit can be sold to a stranger.**
Not the buyer. This document answers one question: can an individual Office 365
or Gmail user — with no MSP, no tenant admin, and no willingness to talk to a
human first — get this connector working on their own mailbox, and what does it
cost rTech to make that legal?

**Version:** 0.1.0 · **Status: PROPOSED / research.** Nothing here has been
executed. No self-serve install has been attempted against a real tenant or a
real `@gmail.com` account, no app has been submitted to either vendor's
verification process, and no assessor has been quoted. Every vendor claim below
carries its source URL; every claim without one is marked **UNVERIFIED** and
must not be repeated to a buyer as fact.

**Scope boundary.** The ADMIN path — MSP as tenant admin, grants across a firm —
is `POSTURE-RUNBOOK.md`, written by a separate lane. This document does not
cover it and does not duplicate it. The assisted lane-1 install is
`INSTALL-RUNBOOK.md`. This is the third path: **nobody is in the room but the
buyer.**

---

## 1. The two audiences, side by side

The divergence is not gradual. It happens at the **first** screen, and it
happens for a different reason on each platform.

| | **Managed tenant (admin present)** | **Individual / self-serve (no admin)** |
| --- | --- | --- |
| **Who registers the app** | The client's own IT, in the client's own tenant (`INSTALL-RUNBOOK.md` §2.1, §3.1). rTech never supplies a shared client ID. | Nobody. A self-serve buyer will not create an Entra app registration or a Google Cloud project. **rTech must supply a shared, published, multi-tenant app** — which inverts the kit's current architecture. See §5. |
| **Who consents** | A tenant admin, once, for the org. Users are never prompted. | The individual user, for their own mailbox only. |
| **Microsoft screen** | Entra admin consent screen; "Consent on behalf of your organization" checkbox. | Standard delegated consent prompt — **or** `AADSTS90094` / "Approval required," which is the likely outcome. §2. |
| **Google mechanism** | Domain-wide delegation with a service account, scopes typed into Admin console (`INSTALL-RUNBOOK.md` §2.2), or a Marketplace domain install. | Ordinary OAuth consent screen in a browser. **DWD is unavailable** — there is no admin console to grant it in. §3. |
| **Gmail scope set reachable** | `gmail.readonly` + `gmail.modify.restricted` → **scope-layer** no-send guarantee. | `gmail.modify` only → **code-layer** guarantee. `gmail.modify.restricted` is DWD-only and is refused live on an interactive consent screen (`scopes.py:88-97`, proven 2026-08-18). **The self-serve buyer cannot have the strong Gmail posture.** |
| **Verification burden on rTech** | **None.** The app is internal to the client's tenant; the client is their own publisher (`INSTALL-RUNBOOK.md` §9). | **Real, recurring, and gating.** Google restricted-scope verification plus an annual security assessment; Microsoft publisher verification to avoid a step-up block. §4. |
| **Failure mode when blocked** | Admin fixes it in their own console. | **The buyer is stuck and leaves.** There is nobody to escalate to inside the product. |

**The one-sentence version.** The managed path pushes every cost onto the
client's admin, which is why it needs one. The self-serve path pushes every cost
onto rTech — a published app, a verified publisher, an annual paid security
assessment — and buys a weaker Gmail guarantee in exchange.

---

## 2. Microsoft self-serve path, step by step

### 2.1 What the user does

Assuming rTech ships a published multi-tenant app (§5 says it currently does
not):

1. User installs the connector and runs `uv run inbox-zero-connector auth microsoft`.
2. The connector starts the **device code flow** (`providers/outlook.py:543-565`).
   The terminal prints a code and a URL; the user opens it in a browser.
3. User signs in with their work account.
4. **The consent prompt appears — or it does not.** This is the fork.
5. On success, the token lands in the OS keychain (`auth.py:62-81`). Nothing is
   written to disk.

### 2.2 Where it breaks: user consent is disabled or restricted

This is the single most likely self-serve failure on Microsoft, and it is not an
edge case — it is the **default**.

Microsoft's own documentation describes the three tenant settings:

> - *You can disable user consent*. Users can't grant permissions to
>   applications. […]
> - *Users can consent to applications from verified publishers or your
>   organization, but only for permissions you select*. […] Users can consent
>   only to the permissions classified as *low impact*.
> - *Users can consent to all applications*.

— [Overview of user and admin consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/user-admin-consent-overview)

Two facts make the middle option lethal for this kit:

- **It is the recommended posture, and Microsoft steers new tenants to it.**
  The configure-user-consent page states: "we recommend that you allow user
  consent only for applications that have been published by a
  [verified publisher](https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview)."
  ([source](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent))
- **`Mail.ReadWrite` is not low-impact.** Only permissions an admin has
  explicitly *classified* as low impact are user-consentable under that policy,
  and the classification list is opt-in per tenant
  ([permission classifications](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-permission-classifications)).
  No admin classifies whole-mailbox read/write as low impact.

**A distinction that matters and is easy to get wrong.** `Mail.ReadWrite`
delegated is **not** flagged "admin consent required" at the registration level
— that flag reads **No**
([Graph permissions mirror](https://graphpermissions.merill.net/permission/Mail.ReadWrite),
permission GUID `024d486e-b451-40bb-833d-3e66d98c5c73`). So the block is not a
property of the permission; it is a property of the **tenant's consent policy**.
Practically the user is blocked either way, but the correct sentence to a
technical buyer is *"your tenant's policy blocks it,"* never *"Microsoft
requires an admin for this permission."* The second is false and a security
reviewer will catch it.

**UNVERIFIED:** search results reported a July 2025 migration of tenants still on
the wide-open legacy default onto the restrictive policy. Microsoft's own
configure-user-consent page was read this session and does **not** state a
default or describe that migration. Do not quote a date or a migration to
anyone.

### 2.3 The exact error the user sees

Two different messages, depending on one tenant setting.

**If the admin consent workflow IS enabled** — the user gets a path forward:

> An **Approval required** message appears. The user types a justification for
> needing access to the application and then selects "Request approval." A
> **Request sent** message confirms that the request was submitted to the admin.

— [Overview of user and admin consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/user-admin-consent-overview)

**If it is NOT enabled** — the user gets a dead end, verbatim:

> **AADSTS90094**: &lt;clientAppDisplayName&gt; needs permission to access
> resources in your organization that only an admin can grant. Request an admin
> to grant permission to this app before you can use it.

— [Configure risk-based step-up consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-risk-based-step-up-consent)

A related code the operator will also see in support tickets is **AADSTS65001**
("The user or administrator has not consented to use the application with ID
…"), which Microsoft's troubleshooting material attributes to several causes
including user consent being disabled at the tenant level
([troubleshoot consent issues](https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/app-integration/troubleshoot-consent-issues)).
Treat 90094 as the self-serve signature and 65001 as the generic one.

### 2.4 Risk-based step-up — a second, independent block

Even where user consent is *allowed*, a brand-new multi-tenant app can be
blocked on risk alone:

> consent requests for newly registered multitenant apps that aren't publisher
> verified and require nonbasic permissions are considered risky. If a risky
> user consent request is detected, the request requires a "step-up" to admin
> consent instead. **This step-up capability is enabled by default**

— [Configure risk-based step-up consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-risk-based-step-up-consent)

The publisher-verification page states the same rule from the other side:
since November 2020, "users can't consent to most newly registered multitenant
apps that *aren't* publisher verified" where the app requests permissions
"beyond the basic sign-in and read user profile"
([source](https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview)).
`Mail.ReadWrite` is emphatically beyond basic sign-in.

**Consequence: Microsoft publisher verification is not optional for a self-serve
Microsoft lane.** Without it, a default-configured tenant blocks the consent
before the user's own permissions are even consulted.

### 2.5 The user's actual options when blocked

In descending order of realism:

1. **Request approval** from their admin, if the workflow is enabled — one
   click, and then they wait. rTech's product is now gated on a stranger's IT
   department answering an email.
2. **Ask their admin directly**, out of band, if the workflow is not enabled.
   The user must know who that is and be willing to ask.
3. **Use a personal Microsoft account instead.** Not a fix for a business
   mailbox, and the `organizations` authority the connector currently pins
   excludes consumer accounts entirely (§5, finding 3).
4. **Nothing.** They leave. This is the honest modal outcome and the reason the
   self-serve Microsoft lane cannot be sold as frictionless.

**The design consequence.** A self-serve Microsoft install must **detect
AADSTS90094 and tell the user what it means in their own words**, including who
to ask and what to ask for. Today the connector does not (§5, finding 5).

---

## 3. Google self-serve path, step by step

### 3.1 What the user does

1. User runs `uv run inbox-zero-connector auth google`.
2. The connector runs `InstalledAppFlow.run_local_server(port=0, prompt="consent")`
   (`providers/gmail.py:234-240`) — a browser opens against a local loopback
   redirect.
3. User picks their Google account.
4. **The unverified-app screen appears**, if rTech's app is not verified. §3.2.
5. User grants the scope. For self-serve this is `gmail.modify` and nothing else
   (`scopes.py:170-178`).
6. Token to the OS keychain.

### 3.2 The unverified-app screen — what the user must click through

An app requesting a sensitive or restricted scope that has not completed
verification shows a warning screen. Google's help documentation describes the
condition and the bypass; the screen is reached via an **Advanced** link and
then a "Go to *Project Name* (unsafe)" link
([Unverified apps, Google Cloud Platform Console Help](https://support.google.com/cloud/answer/7454865?hl=en)).

**UNVERIFIED:** the *exact verbatim heading* of that screen. The commonly quoted
string is "Google hasn't verified this app," which appears in third-party
write-ups and in search summaries, but the fetch of Google's own help page
returned an image reference rather than the literal text, so it was not
confirmed from a primary source this session. Do not put quotation marks around
that sentence in buyer-facing copy until someone reads it off a live screen.

**The cap that kills a trial funnel.** Google's help page states the limit for
apps showing that screen as **"100 new users in total, after the app presents
the unverified app screen"**
([source](https://support.google.com/cloud/answer/7454865?hl=en)). An unverified
app therefore has a hard ceiling of roughly one hundred lifetime self-serve
users — usable for a pilot, not for a product.

### 3.3 Testing versus production, and the seven-day trap

Google's audience documentation is explicit and both numbers matter:

> Projects configured with a publishing status of Testing are limited to up to
> 100 test users listed in the OAuth consent screen.

> Projects configured with a publishing status of In production are available to
> any user with a Google Account.

> Authorizations by a test user will expire seven days from the time of consent
> […] that token will also expire.

— [Manage app audience](https://support.google.com/cloud/answer/15549945?hl=en)

**Read the third one twice.** In Testing status, a self-serve user's
authorization dies after seven days and the connector stops working with no
action by anyone. A trial built on Testing status silently breaks in a week and
the buyer concludes the product is broken. Any real self-serve Google lane must
be **In production**, which means verified, which means §4.

Also from that page: **Internal** user type limits authorization to members of
one Google Cloud Organization. That is the managed path, not this one. A
self-serve `@gmail.com` buyer requires **External**.

### 3.4 The scope the self-serve buyer actually grants

There is no way around this and it must be said in the sales conversation, not
after it. The connector's own manifest records the finding
(`scopes.py:88-97`): `gmail.modify.restricted` is documented by Google solely
for "a service account that has domain-wide delegation authority," and an
interactive request for it was **refused live on 2026-08-18** with
`Error 400: invalid_scope`.

So the self-serve Gmail user grants `gmail.modify`, which Gmail's send methods
also accept. Per `scopes.enforcement_for`, the honest posture is **code-layer**:
the token can send; this connector has no send code path, and two scanners
assert that every build. That is a real control and a weaker claim, and the
distinction is exactly what `INSTALL-RUNBOOK.md` §1 forbids flattening.

**A self-serve Gmail buyer can never be sold the scope-layer sentence.**

---

## 4. Verification and publishing — what it costs rTech

This is the section the operator most needs to be true, so it is deliberately
thin where sourcing is thin.

### 4.1 Microsoft

| Item | Requirement | Cost | Timeline |
| --- | --- | --- | --- |
| **Multi-tenant registration** | App registered as "Accounts in any organizational directory"; Application ID URI globally unique. Authority `organizations` for business tenants. | None | Immediate |
| **Publisher verification** | A verified **Microsoft AI Cloud Partner Program** account (formerly MPN) that is the partner global account; app registered with a work/school account; publisher domain set and **not** `*.onmicrosoft.com`; domain matching between the CPP verification email and the publisher domain; initiator holds Application Administrator or Cloud Application Administrator in Entra **and** CPP Partner Admin or Account Admin in Partner Center; MFA; ToU consent. | **Free.** Microsoft states: "Microsoft doesn't charge developers for publisher verification. No license is required to become a verified publisher." | "Developers who have already met these requirements can be verified in minutes." |

Source for every cell in the publisher-verification row:
[Publisher verification overview](https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview).

**The catch is the prerequisite, not the verification.** Verification itself is
minutes and free; obtaining a *verified* CPP partner global account is a
separate onboarding rTech has not done. **UNVERIFIED:** the time and effort to
get a CPP account verified — not researched this session, and Microsoft's
"no charges are associated with completing the prerequisites" line refers to
charges, not to elapsed time.

Also noted so nobody assumes it: publisher verification is **not** supported in
national clouds, and the blue badge implies nothing about quality or compliance
(same source).

### 4.2 Google — the expensive one

`gmail.modify` is a **restricted** scope. Two separate gates follow.

**Gate 1 — restricted scope verification.** Apps requesting restricted scopes
must "demonstrate they're a permitted application type, submit to additional
reviews, possibly undergo security assessment, comply with branding
requirements, verify domain ownership, and provide accurate OAuth consent screen
information." Google states plainly: **"the restricted scopes verification
process can potentially take several weeks to complete."** Brand verification
alone "typically takes 2-3 business days" when branding has changed.
([Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification))

**Gate 2 — the security assessment (CASA).** The trigger is the sentence that
decides rTech's exposure:

> Every app that requests access to Google users' restricted data and has the
> ability to access data from or through a third-party server must go through a
> security assessment from Google-empanelled security assessors.

— same source

**This is the one genuinely favourable fact in the whole document.** The kit
stores nothing server-side: tokens live in the OS keychain (`auth.py`), the
ledger is a local file, and rTech is absent from the token chain. On the plain
reading of that sentence, a purely local connector does not have "the ability to
access data from or through a third-party server" and the assessment does not
attach.

**Do not bank that yet.** Three reasons:

1. It is rTech's own reading of one sentence, not a determination by Google. The
   determination is made during review, by Google, about this specific app.
   Mark it **PROPOSED** until a submission comes back.
2. It depends entirely on the architecture actually holding — which
   `CAPABILITIES.md` §7 already lists as an open internal verification item
   ("whether the architecture genuinely keeps message bodies in-tenant"). If a
   hosted watcher, a remote MCP endpoint, or any rTech-side component is ever
   added, the exemption evaporates and the cost lands.
3. Recertification is annual regardless of tier once it applies: apps "must be
   reverified for compliance and complete a security assessment at least every
   12 months" after the assessor's Letter of Assessment approval date
   (search-summary attribution to Google's documentation — **UNVERIFIED**
   against a primary page fetched this session).

**Cost of a CASA assessment: UNVERIFIED, and deliberately left blank.** Google's
restricted-scope page contains **no** statement of cost or of who pays. The
App Defense Alliance CASA pages fetched this session state the tiering is
risk-based across AL1/AL2 and that revalidation is annual, but likewise carry
**no cost figure**. A "$500+ per year" number surfaced only in a community
forum summary. **Do not quote any dollar figure to the operator or to a client.** If the
assessment turns out to apply, get a written quote from an ADA-authorized
assessor before pricing the self-serve tier.

**UNVERIFIED:** whether AL1 or AL2 would apply to this app, and whether any
self-scan option exists. The ADA tiering page fetched this session describes
both AL1 and AL2 as lab-tested and validated, which contradicts the widely
repeated claim that AL2 is a free self-scan. Do not repeat the self-scan claim.

### 4.3 What this means for the offer

| Lane | rTech verification cost | Gmail posture | Sellable to a stranger today? |
| --- | --- | --- | --- |
| Assisted / managed (`INSTALL-RUNBOOK.md`) | **Zero** — client's own app in client's own tenant | scope-layer (DWD) | No — needs an admin and an afternoon |
| Self-serve Microsoft | CPP account + publisher verification (free, minutes, after CPP onboarding) | n/a | **No** — no shared app exists (§5) |
| Self-serve Google | Restricted-scope verification, several weeks; assessment cost UNVERIFIED | code-layer only | **No** — no published app, no verification |

The per-client-registration design was chosen precisely to avoid all of this
(`INSTALL-RUNBOOK.md` §9: "no restricted-scope verification burden inherited
from rTech"). Self-serve is the decision to take that burden on deliberately.
It is a real cost with a several-week lead time on Google, and it should be
started before it is promised to anyone.

---

## 5. What the connector must do differently — findings, not fixes

Read from the source this session. **Listed for a later lane. Nothing here is
fixed by this document.**

**Finding 1 — the connector has no app of its own, by design.**
`config.py:673-731` (`CredentialSettings`) requires the buyer to supply
`google_client_id`, `google_client_secret`, and `microsoft_client_id` from the
environment. `require_google` and `require_microsoft` raise a `ConfigError`
pointing the user at "the OAuth client you created in your own Google Cloud
project" / "the Entra app registration you created in your own tenant." A
self-serve buyer will not do this, and the error message tells them to. **This
is the single largest self-serve blocker and it is architectural, not a bug** —
`INSTALL-RUNBOOK.md` §9 states "Never a shared rTech client ID" as an operator
rule. Self-serve requires reversing that rule for a shipped, verified app, and
that reversal puts rTech back into the token chain, which the vendor-agnostic
doctrine (`auth.py:22-23`) exists to prevent. **This is a business decision, not
an engineering one.**

**Finding 2 — the Gmail strong posture is admin-only and the code knows it.**
`scopes.py:106-121` (`GMAIL_DWD_TRIAGE_SCOPES`) documents itself as "REACHABLE
ONLY VIA DWD, and the DWD service-account flow is NOT YET IMPLEMENTED."
`scopes_for` (`scopes.py:300-307`) returns `GMAIL_INTERACTIVE_SCOPES` — bare
`gmail.modify` — for any interactive request. Self-serve is therefore
permanently code-layer on Gmail. Correct behaviour; it just needs to reach buyer
copy for this lane.

**Finding 3 — the Microsoft authority defaults to `organizations`, excluding
consumer accounts.** `config.py:686`: `microsoft_tenant_id: str = "organizations"`,
composed into the authority at `auth.py:153-156`. Microsoft's documentation is
explicit that `organizations` signs in "users with work and school accounts"
while `consumers` is personal Microsoft accounts only and `common` is both
([MSAL client application configuration](https://learn.microsoft.com/en-us/entra/identity-platform/msal-client-application-configuration)).
A self-serve buyer on a personal Outlook.com mailbox is silently out of scope
today. Whether that is desirable is a product call — but it is currently an
undeclared default, not a stated decision.

**Finding 4 — `auth` is a deliberate CLI step that assumes a terminal and a
human.** `cli.py:13-16` states the interactive flow "opens a browser or prints a
device code, neither of which works from inside a tool call under a host AI, so
the MCP server never triggers it." `providers/outlook.py:543-546` refuses the
device flow when there is no terminal. This is correct and safe, but it means
self-serve onboarding is a command line, which is a different buyer than the one
this requirement describes.

**Finding 5 — no consent-failure handling exists for the self-serve errors.** A
grep of the provider and auth modules found no handling of `AADSTS90094`,
`AADSTS65001`, `consent_required`, or `invalid_scope` as *named* conditions with
user-facing guidance. The one place the connector does this well is the DWD
scope refusal, which is documented in `scopes.py` prose rather than caught at
runtime. A self-serve buyer hitting §2.3 gets a raw library error. **This is the
highest-value, lowest-risk self-serve fix and it is independent of Finding 1.**

**Finding 6 — `init-config` presumes an interview that has already happened.**
`cli.py:_cmd_init_config` requires `--owner-domain` and takes `--sales-label`
repeats; `INSTALL-RUNBOOK.md` §6.2 states plainly that skipping it leaves the
connector with no owner domain, so "drafting cannot start." In the assisted lane
an operator runs this interview. In self-serve nobody does. The kit's own
`installer.md` is designed to conduct it via the host AI, so the path may
already exist — **UNVERIFIED**, not traced this session.

**Finding 7 — the strong posture assumes a tenant-side hardening step nobody
will perform.** `INSTALL-RUNBOOK.md` §4 makes admin-side send denial "a required
checklist item on every lane-1 install," and `scopes.py:241-247` names
tenant-side hardening as what makes the Microsoft claim structural rather than
dynamic-consent-bypassable. A self-serve user cannot do any of it. Self-serve
security copy must therefore rest on the token and the code alone, and must not
inherit sentences written for a hardened tenant.

---

## 6. Read-only as the trial posture

**The argument for it.** A read-only grant is the only version of this product
that can be handed to a stranger with a genuinely zero-risk claim. On Microsoft
it is `Mail.Read`; on Google, `gmail.readonly`. Both are strictly less privilege
than the connector requests today, and `CAPABILITIES.md` §5 already ranks them
below the held rung. Nothing can be moved, mislabelled, deleted, or drafted.
Undo is unnecessary because nothing happened. The buyer's worst case is that
they wasted ten minutes.

**And it demonstrates the thing that actually differentiates the product.** Per
`CAPABILITIES.md` §2.2, the Business-Action / Business-Waiting distinction —
"the strongest no in the brief," the one claim that survived an unflattering
audit — is a **judgment**, not a mutation. Classification needs the message
body, which read-only provides. `classify.py` performs no I/O at all: it is a
pure function from a message to a `Classification`. The mailbox-profiling
artifact (`CAPABILITIES.md` §2.8) is likewise read-only and the rules-brief
already calls it "a genuinely good artifact to put in front of a prospect,
because it shows the firm its own mail distribution before anything has been
changed."

So a read-only trial can produce, on the stranger's own real mail: a coverage
report, a classified inbox survey, a daily digest, and the Action/Waiting split
on threads they recognise. That is the demo.

**What is missing, stated honestly.** Three things, and the third is the real
one:

1. **No filing and no categories.** The user reads a report; their mailbox is
   unchanged. `file_message` and the proposed category write both need the write
   scope.
2. **No drafts.** Out of reach on both providers.
3. **The persistence gap — and this is what read-only genuinely cannot buy.**
   The product's value is that the classification *stays* on the message where
   the user's own rules, searches, and Quick Steps can act on it
   (`CAPABILITIES.md` §3.1, §3.5). A read-only trial produces a report that is
   true when generated and stale an hour later. The user sees the judgment was
   right; they do not experience the workflow. Converting that into a purchase
   depends on the report being persuasive on its own, which is **UNVERIFIED and
   is a marketing hypothesis, not a technical finding.**

**One consent-flow fact that cuts against read-only, and it is decisive on
Google.** `gmail.readonly` is **also a restricted scope** — it appears in the
connector's manifest at `scopes.py:62-71` and is part of the strong DWD set.
Dropping to read-only therefore does **not** escape §4.2: the same
restricted-scope verification and the same several-week timeline apply. Read-only
buys a safety and consent-friction improvement, **not** a verification shortcut.

On Microsoft it does help, and materially: `Mail.Read` is a smaller ask on a
consent screen than `Mail.ReadWrite`. **UNVERIFIED:** whether `Mail.Read` clears
any tenant's low-impact classification bar. It almost certainly does not by
default — classification is opt-in per tenant — so §2.2 likely blocks read-only
self-serve on Microsoft too. That needs one live test against a default tenant
before anyone claims read-only is easier to get consented.

**Recommendation, stated as a recommendation.** Ship the trial as read-only,
because zero-risk is true and checkable and because it costs nothing on the
Google verification path that the write scope does not also cost. Do not expect
it to dodge verification, and do not expect it to dodge the Microsoft consent
block. Its value is that a stranger can say yes without thinking hard — not that
it is cheaper to publish.

---

## 7. What must be true before this lane is sold

Nothing below is done. In dependency order:

1. **A decision on Finding 1.** Shared rTech app registration, or no self-serve
   lane. Everything else is downstream and none of it should start first.
2. **Microsoft: a verified CPP partner global account**, then publisher
   verification (free, minutes once the CPP account is verified).
3. **Google: restricted-scope verification submitted**, budgeting several weeks
   of calendar time before the lane can be promised to anyone.
4. **A written determination on the CASA trigger** — from Google during review,
   not from this document — and a written assessor quote if it applies.
5. **Consent-failure handling** (Finding 5) so a blocked user is told what
   happened and what to ask for.
6. **One live test each**: a default-configured M365 tenant and a personal
   `@gmail.com`, both run end to end, with the exact screens captured into
   an evidence folder. Every claim in this document that is currently sourced only to
   vendor documentation becomes RUNNING or gets corrected at that point.

Until item 6, this document is research and the lane does not exist.
