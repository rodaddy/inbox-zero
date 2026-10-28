# Inbox Zero Connector — The Questions We Did Not Ask

**Status: WRITTEN, not verified against a live tenant.** Every vendor claim below
carries a primary-source URL. Every code claim carries a `file:line`. Nothing here
has been reproduced against a real M365 or Workspace tenant, and several findings
say explicitly that a live test is the only way to close them.

**Audience:** the operator deciding what to fix before the next paying install, and
the person who has to answer a law firm's IT director without guessing.

**Why this document exists.** A prior round of blockers surfaced late — tenant
consent policy killing self-serve M365 install, Google Testing-status tokens
expiring in 7 days, `gmail.readonly` being restricted, no compose-without-send scope
on Gmail, no send-without-forward on Gmail, both providers discarding granted scopes
at auth, and rules living on `MailboxSettings.ReadWrite` rather than `Mail.*`. Those
are known and are **not** repeated here. This is the next set, found by asking the
vendor docs the questions the kit had not yet asked, and by reading the connector
source rather than its documentation.

**The four buyers**, referenced throughout by number:

1. **MSP** — manages many law-firm M365 tenants
2. **M365 owner-admin** — self-managed single tenant
3. **Workspace admin** — Google Workspace tenant
4. **Consumer** — `@gmail.com`, no admin

**Tags:** `CODE-FIX` (source change) · `DOC-FIX` (runbook/capability text) ·
`SALES-QUESTION` (qualify before quoting) · `PLATFORM-FACT` (unfixable; disclose)

---

## Ranked by cost if discovered in front of a client

| # | Finding | Buyers | Tag |
|---|---|---|---|
| 1 | Moving mail changes its retention outcome on BOTH platforms | 1,2,3 | `CODE-FIX` + `DOC-FIX` |
| 2 | "Every mutation is reversible" is false, and it is written into the product | all | `DOC-FIX` |
| 3 | The MSP story does not exist in code — one token slot per provider | 1 | `CODE-FIX` |
| 4 | Outlook `poll_changes` is unimplemented; the watcher is unreachable | 1,2 | `CODE-FIX` |
| 5 | Public-client device-flow auth forfeits the CA protection a law firm triggers | 1,2 | `CODE-FIX` |
| 6 | No CAE claims-challenge handling — the connector dies on an opaque 401 | 1,2 | `CODE-FIX` |
| 7 | Graph mutable IDs: no `ImmutableId` header, and `/move` re-IDs the message | 1,2 | `CODE-FIX` |
| 8 | Shared mailboxes are architecturally impossible in the current code | 1,2 | `CODE-FIX` |
| 9 | SKUs where the product cannot work at all (F1, Essentials) | 1,2,3 | `SALES-QUESTION` |
| 10 | Graph folder listing is capped at 200 with no pagination | 1,2 | `CODE-FIX` |
| 11 | `Retry-After` is ignored; 3 attempts, 8s max backoff | all | `CODE-FIX` |
| 12 | Google's 100-token silent eviction and Gmail password-change revocation | 3,4 | `PLATFORM-FACT` |
| 13 | Archive moves remove mail from Graph's reach entirely | 1,2 | `PLATFORM-FACT` |
| 14 | Multi-tenant publisher cost is asymmetric — Google charges annually | 1 | `SALES-QUESTION` |
| 15 | Gmail per-user quota caps throughput at ~222 msgs/min | 1,3,4 | `DOC-FIX` |
| 16 | Ledger and logs never rotate | all | `CODE-FIX` |

---

## 1. Moving mail changes its retention outcome. On both platforms.

`CODE-FIX` + `DOC-FIX` · Buyers 1, 2, 3 · **The worst finding in this document.**

**What we did not ask:** whether filing a message into a connector-created folder
changes how long the firm keeps it.

**The answer — Microsoft.** Retention tags are inherited from the destination folder:

> "A mailbox item moved from one folder to another **inherits any tags applied to the
> folder to which it's moved**. If an item is moved to a folder that doesn't have a tag
> assigned, **the DPT is applied to it**."
> — [Retention tags and retention policies in Exchange Online](https://learn.microsoft.com/en-us/exchange/security-and-compliance/messaging-records-management/retention-tags-and-policies)

Retention Policy Tags attach to **default folders only** — Inbox, Deleted Items, Sent
Items ([default folders](https://learn.microsoft.com/en-us/exchange/security-and-compliance/messaging-records-management/default-folders)).
A connector-created folder **cannot carry one**. So a message moved out of Inbox loses
the Inbox RPT and falls through to the mailbox-wide Default Policy Tag. If the DPT
action is `Move to Archive`, the move pushes mail into the archive — which per finding
13 is invisible to Graph. If the DPT is `Permanently Delete`, the move can cause a
message to be deleted **sooner than the firm's policy intended**.

**The answer — Google.** Worse, because the clock reset is explicit:

> "Retention rules based on labels use the date of the **most recently labeled message
> in a thread** for retention purposes." … "If you create a custom retention rule based
> on a Gmail label, the rule doesn't apply to any nested sublabels."
> — [Retain Gmail messages with Vault](https://support.google.com/vault/answer/2535539)

So the connector's own labeling action can **reset the retention clock** for a whole
thread, and a nested label (`rTech/Triage/Urgent`) can drop messages out of a rule the
firm believes covers them.

**What the code does.** The good news first: `watcher.py:78` fixes `WRITE_METHOD` to
`tag_message` and `watcher.py:86-90` puts `file_message` in `FORBIDDEN_METHODS`, so the
**unattended loop is categorize-only and therefore retention-safe**. That is a genuinely
good design decision and it should be said out loud in the sales conversation.

The bad news: `file_message` is a live MCP tool at `server.py:654`, described at
`server.py:7` as "move into a taxonomy folder". `outlook.py:999` issues
`POST /me/messages/{id}/move` with a `destinationId` payload. So the AI-driven path does
exactly the destructive thing
the watcher refuses to do, and nothing warns anyone.

**Cost if found late:** a law firm discovers the connector shortened retention on
client-matter mail. That is not a bug report, it is a professional-responsibility
incident and an MSP-relationship-ending event.

**Fix:** (a) `DOC-FIX` — state plainly that categorize-in-place is retention-neutral and
move is retention-**changing**, and make categorize the recommended default posture;
(b) `CODE-FIX` — `file_message` should warn or require explicit acknowledgement, and the
install runbook must capture the tenant's DPT and any label-scoped Vault rules;
(c) flat, non-nested labels on Gmail. **UNVERIFIED:** whether Graph can stamp a personal
retention tag on a connector-created folder — no such API surface was found on
`mailFolder`.

---

## 2. "Every mutation is reversible" is asserted in the product and is not true

`DOC-FIX` · all buyers

**What we did not ask:** whether our own reversibility claim survives contact with
retention and archive policy.

`server.py:53` states: *"EVERY MUTATION IS REVERSIBLE. Folder moves, flags, and unsent
drafts."* Finding 1 shows a folder move can change retention outcome; finding 13 shows a
move can trigger an archive transition that **changes the message's immutable ID**
([immutable ID doc](https://learn.microsoft.com/en-us/graph/outlook-immutable-id) — "the
immutable ID changes if: The user moves the item to an archive mailbox"). Moving it back
does not restore the original ID, and the ledger row now points at an entity that no
longer resolves.

**Cost if found late:** this sentence is in the tool description the host AI reads and
would end up quoted in a security review. Being caught overstating a safety property is
more expensive than the property itself.

**Fix:** narrow the claim to what holds — "no mutation deletes mail; moves and labels
are reversible in position but may alter retention treatment."

---

## 3. The MSP story does not exist in code — one token slot per provider

`CODE-FIX` · Buyer 1

**What we did not ask:** what happens on the second mailbox.

`auth.py:58-59` keys credentials as `_key(provider) = f"{provider}-token"`. There is no
mailbox, tenant, or account component. Authenticating a second Microsoft mailbox
**overwrites the first**. `paths.py` re-exports a single `config_dir`/`data_dir`/
`ledger_path` with no per-mailbox namespacing, so config, watch state, and ledger are
also single-tenant by construction.

CAPABILITIES.md:296-298 is honest that there is no MSP control plane, but the gap is
deeper than "no dashboard": the connector cannot hold two mailboxes' credentials
simultaneously on one host.

**Cost if found late:** the MSP is buyer #1, the one with 40 mailboxes. Discovering this
during the second install destroys the deal and the reference.

**Fix:** key the keyring entry and all state paths by account identity. Until then, the
deployment unit is one mailbox per host/user profile, and that must be said in writing.

---

## 4. Outlook `poll_changes` is unimplemented and the watcher is unreachable

`CODE-FIX` · Buyers 1, 2

**What we did not ask:** whether the unattended product runs at all on the platform most
law firms use.

`outlook.py:1013-1016` raises: *"Outlook poll_changes needs one delta stream per in-scope
folder (Graph has no mailbox-wide delta). Use Gmail for now."* So **the watcher does not
work on Microsoft at all.**

Separately, `watcher.py` is not wired to anything: no `run_round`/`watcher` reference
exists in either `cli.py` or `server.py`. There is no `watch` command and no daemon loop.
The "unattended watcher" has no runner on **any** platform.

Microsoft's own docs confirm the shape of the missing work — Outlook delta tokens have no
fixed lifetime (*"the upper limit isn't fixed; it's dependent on the size of the internal
delta token cache"*) and `410 Gone` signals a forced full resync, documented as triggered
by *"internal maintenance or migration of the target tenant"*
([delta query overview](https://learn.microsoft.com/en-us/graph/delta-query-overview)).
Per-folder streams mean one independently-expiring token per folder.

**Cost if found late:** the demo is Gmail, the buyer is M365, and the gap appears at
install.

**Fix:** implement per-folder delta with `410`/`syncStateNotFound` resync, and wire an
entrypoint. Until then, `DOC-FIX`: the status table must say unattended operation is
Gmail-only and manual-invocation-only.

---

## 5. Public-client device flow forfeits the one CA protection a law firm triggers

`CODE-FIX` · Buyers 1, 2

**What we did not ask:** whether our auth client type survives a law firm's Conditional
Access posture.

`outlook.py:527` constructs `msal.PublicClientApplication`, and `outlook.py:596` uses
`acquire_token_by_device_flow`. That makes this a **public client**.

Microsoft documents a deferral that exists only for confidential clients:

> "**On confidential clients, the system defers sign-in frequency enforcement on
> non-interactive sign-ins until the next interactive sign-in.**"
> — [Conditional Access session lifetime](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-session-lifetime)

A confidential-client daemon is documented as surviving a sign-in-frequency policy. A
public client gets no such statement. **UNVERIFIED** whether a public-client daemon is
hard-blocked by SIF — the docs state the deferral only for browsers and confidential
clients, and are silent on the rest. That silence is exactly the thing to test before
selling into a firm with CA.

Also relevant: refresh tokens issued to a redirect URI registered as `spa` **expire in 24
hours** rather than 90 days
([refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens)).
Register `web`, never `spa`.

Baseline default is otherwise fine: 90-day rolling inactivity, and refresh tokens
*"replace themselves with a fresh token upon every use"* (same page). A connector polling
more often than every 90 days never hits inactivity expiry. **The token default is not
the risk — the tenant's CA posture is.**

Two more from the same research worth putting in the qualification script: token
protection / token binding *"ensur[es] only device bound sign-in session tokens, like
Primary Refresh Tokens (PRTs), are accepted"* and **supports Exchange Online**
([token protection](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-token-protection))
— a Linux container holding a refresh token has no PRT and is dead on arrival in such a
tenant. And a named-location/IP policy is enforced *"instant[ly]"* via CAE, so a hosted
connector's egress IP must be a trusted named location.

**Fix:** `CODE-FIX` to a confidential client where the deployment allows it;
`SALES-QUESTION` — ask for the tenant's CA policy list before quoting.

---

## 6. No CAE claims-challenge handling — the connector stops on an opaque 401

`CODE-FIX` · Buyers 1, 2

**What we did not ask:** what a Continuous Access Evaluation revocation looks like to
this code.

CAE is on by default for critical events *"in any tenant"* and does not require CA
policies or P1/P2; new tenants are *"Auto Enabled for CAE: Yes"*. Its enforcement
mechanism is a **401 on a token that has not expired**:

> "With CAE, a resource provider can reject a token when it isn't expired… a mechanism
> called claim challenge indicates that the token was rejected and a new access token
> needs to be issued."
> — [Continuous access evaluation](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation)

Critical events include account disable/delete, password change/reset, MFA enablement,
admin token revocation, and high user risk — with latency *"up to 15 minutes"*.

**What the code does.** Nothing. There is no `401`, `WWW-Authenticate`, or `claims`
handling anywhere in the source (`outlook.py:1154` matches `id_token_claims`, which is
unrelated). `retry.py:56` deliberately excludes 401/403 from `RETRYABLE_STATUS`, so a CAE
401 becomes a non-retryable generic `ProviderError` (`outlook_errors.py:78-126`) carrying
only `HTTP 401`. The connector stops, and the operator gets no indication that
re-consent is the remedy.

**Cost if found late:** a firm resets a user's password, the connector silently stops
triaging, and nobody notices for days.

**Fix:** parse the `WWW-Authenticate` claims challenge, pass claims back through MSAL,
and — at minimum — emit an actionable "re-authentication required" alert rather than a
bare 401. Microsoft's implementation guide is
[app resilience and CAE](https://learn.microsoft.com/en-us/entra/identity-platform/app-resilience-continuous-access-evaluation).

---

## 7. Graph mutable IDs: no `ImmutableId` header, and `/move` re-IDs the message

`CODE-FIX` · Buyers 1, 2

**What we did not ask:** whether the ID we store in the ledger still resolves tomorrow.

Microsoft: *"In general, do not assume that **message** and **mailfolder** IDs are unique
and always remain the same within a mailbox. They might change after certain actions such
as copy or move."*
([mail API overview](https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview?view=graph-rest-1.0)).
The remedy is the `Prefer: IdType="ImmutableId"` header, which the docs say *"you must
include with every API request"*
([immutable IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)).

**What the code does.** The header is never sent — the only match for "immutable" in the
adapter is a comment at `outlook.py:160`. The code is aware of the consequence:
`outlook.py:996-1005` documents that `/move` is copy-then-delete producing a **new id**,
and falls back to the old id when the response has no body (`new_id = moved.id or
message_id`) — writing a knowingly-dead id into the ledger.

This compounds with the replay guard: `watchstate.py:421` keys `seen_message` on message
id with `SEEN_LIMIT = 500` (`watchstate.py:124`). An id that changes under the guard
defeats deduplication — the same message can be processed twice with no error.

**Fix:** send `Prefer: IdType="ImmutableId"` on every Graph request. Note the migration
cost documented by Microsoft: existing subscriptions must be deleted and recreated to
switch ID types.

---

## 8. Shared mailboxes are architecturally impossible in the current code

`CODE-FIX` · Buyers 1, 2

**What we did not ask:** what happens when the firm points us at `intake@firm.com`.

Graph **does** support delegated shared-mailbox access, via `Mail.Read.Shared` /
`Mail.ReadWrite.Shared` and the **other-user path**:
`GET users/{upn}/mailfolders('Inbox')/messages`
([shared and delegated folders](https://learn.microsoft.com/en-us/graph/outlook-share-messages-folders)).

**What the code does.** Every endpoint is hardcoded `/me/*` — `outlook.py:30-32` inventories
them, and `outlook.py:986` builds `("me", "messages", message_id, "move")`. Scopes request
only `Mail.ReadWrite` (`scopes.py:232`), not the `.Shared` variants. There is no code path
that can address another mailbox.

Three further constraints worth knowing before building it:

- **Delegated shared access cannot use webhooks.** *"These two permissions don't support
  subscribing to change notifications on items in shared or delegated folders… use the
  application permission, `Mail.Read`."* (same page). Not a current defect — the connector
  is pull-only, with no `users.watch` or Graph subscription code — but it forecloses a
  future push design on shared mailboxes.
- **Categories are a permission cliff.** `masterCategories` documents **only**
  `MailboxSettings.ReadWrite` with *"Higher privileged permissions: Not available"*
  ([create Outlook category](https://learn.microsoft.com/en-us/graph/api/outlookuser-post-mastercategories?view=graph-rest-1.0)),
  and there is **no `MailboxSettings.*.Shared` variant**. Since the product's core write is
  a category (`outlook_categories.py`), this is the thing to test first.
- **Google has no equivalent at all.** Gmail's delegation API manages the delegate *list*
  only ([manage delegates](https://developers.google.com/workspace/gmail/api/guides/delegate_settings),
  max 25 delegates), and Collaborative Inbox has no message-level API. Multi-mailbox on
  Google means admin-granted domain-wide delegation — a much heavier sale into a small firm.
  **UNVERIFIED:** no vendor page explicitly denies API read of a delegated Gmail mailbox;
  the finding is that no page grants it either.

**UNVERIFIED and needs a live test:** whether move, draft-create, and category writes work
against a shared mailbox with `.Shared` scopes. Microsoft documents only **GET** operations
on that page.

**Cost if found late:** shared intake mailboxes are the single most obvious law-firm use
case. Failing on one looks like a bug, not a boundary.

---

## 9. SKUs where the product cannot work at all

`SALES-QUESTION` · Buyers 1, 2, 3

**What we did not ask:** whether the buyer's license includes a mailbox.

Two disqualifiers, both quotable:

- **Microsoft 365 F1 has no mailbox.** *"Microsoft 365 F1 does not include rights to an
  Exchange mailbox."*
  ([Exchange Online service description](https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-service-description))
- **Google Essentials editions have no Gmail.** *"The Essentials editions do not include
  Gmail."* — covering Enterprise Essentials, Enterprise Essentials Plus, and legacy
  Essentials ([Essentials editions](https://knowledge.workspace.google.com/admin/getting-started/editions/essentials-editions)).
  These buyers say "we have Google Workspace" in good faith.

Also worth capturing at qualification:

- **F3/Kiosk:** no EWS, no Outlook desktop, no delegate access, and *"Role-based
  permissions, role groups and role assignment policies": No* — which may block App RBAC
  scoping. **UNVERIFIED** whether Graph mail specifically is gated by SKU; no vendor page
  states it is. The EWS row is not a Graph answer.
- **Shared mailboxes:** *"A shared mailbox can store up to 50 GB of data without assigning
  a license"*; beyond that, and for litigation hold or archive, an EXO Plan 2 (or Plan 1 +
  Archiving add-on) is required ([about shared mailboxes](https://learn.microsoft.com/en-us/microsoft-365/admin/email/about-shared-mailboxes)).
- **Admin consent is not license-gated**, but Graph **application** permissions require
  **Privileged Role Administrator** — a higher bar than most MSPs assume
  ([grant admin consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/grant-admin-consent)).
- **Business Basic/Standard get Entra ID Free**, so those tenants have **no Conditional
  Access** at all ([Entra licensing](https://learn.microsoft.com/en-us/entra/fundamentals/licensing)) — which makes findings 5 and 6 moot for them and is a genuine simplification.
- **Good news, durable:** **no mail Graph API is metered.** The metered-API list contains
  exactly one row (a SharePoint sensitivity-label call), and Teams export APIs stopped being
  metered on 2025-08-25 ([metered API list](https://learn.microsoft.com/en-us/graph/metered-api-list)).
  Metering an included API *"would be a non-backward compatible change"*
  ([metered API overview](https://learn.microsoft.com/en-us/graph/metered-api-overview)).

**UNVERIFIED, and commercially load-bearing:** Google's admin help pages for
domain-wide delegation and app access control carry **no supported-editions banner at
all**. We cannot state a Google edition matrix from vendor docs. If that answer matters to
a deal, get it from a Workspace reseller in writing rather than from more searching.

---

## 10. Graph folder listing is capped at 200 with no pagination

`CODE-FIX` · Buyers 1, 2

**What we did not ask:** what a matter-numbered folder tree does to folder resolution.

`outlook.py:970-971` resolves folders with a single
`GET /me/mailFolders?$top=MAX_PAGE_SIZE` where `MAX_PAGE_SIZE = 200`
(`outlook.py:95`). There is **no `@odata.nextLink` handling anywhere in the Outlook
adapter** — the string does not appear in the source.

Consequences, both silent: a mailbox with more than 200 folders has folders the connector
cannot see, and `_folder_id` will therefore **create a duplicate folder** that already
exists past the page boundary. The name-keyed `_folder_cache` (`outlook.py:967`) has no
invalidation, so a folder renamed in Outlook mid-session resolves to a stale entry for the
life of the process.

A law firm with per-matter folders is the expected case, not the edge case.

**Contrast, and this one is genuinely fine:** Gmail's `users.labels.list` returns every
label flat in one response with no pagination by API design (`gmail.py:409-411`), so the
Gmail side has no equivalent gap.

**Fix:** follow `@odata.nextLink` in folder enumeration; key the cache by folder id.

---

## 11. `Retry-After` is ignored; 3 attempts, 8-second ceiling

`CODE-FIX` · all buyers

**What we did not ask:** whether our backoff matches what the platforms actually ask for.

Graph returns **429** with a `Retry-After` header in seconds, and Microsoft's guidance is
to honor it; the docs also warn it is not universally present, so exponential backoff is
the fallback ([throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)).

**What the code does.** `Retry-After` appears **nowhere in the source** — verified by
search across `src/`. `retry.py` uses `MAX_ATTEMPTS = 3` (`retry.py:50`) with
`wait_exponential_jitter(initial=1.0, max=8.0)` (`retry.py:53-54`, `retry.py:222`). A Graph
429 asking for a 30-second wait gets retried at ~1s, ~2s, then gives up — three refused
requests that also deepen the throttle.

**Two things the code gets right and should keep:** the 403 carve-out (`retry.py:186`)
correctly treats Gmail's overloaded 403 as retryable only when the body carries a
rate-limit reason, matching Google's documented behavior that quota exhaustion returns
**403 `dailyLimitExceeded`** rather than 429
([handle errors](https://developers.google.com/workspace/gmail/api/guides/handle-errors));
and matching on the response body rather than on the provider name is the right call.

One correction to carry into the code: `RATE_LIMIT_REASONS` (`retry.py:70-77`) includes
`quotaexceeded`, which does **not** appear on Gmail's error-guide page. Harmless, but it is
an invented string sitting next to three real ones.

**Fix:** honor `Retry-After` when present; raise the attempt ceiling for throttling
specifically.

---

## 12. Google's silent token eviction and Gmail-scoped password revocation

`PLATFORM-FACT` · Buyers 3, 4

**What we did not ask:** what kills a Google token that we did not do ourselves.

From [Using OAuth 2.0 to Access Google APIs](https://developers.google.com/identity/protocols/oauth2):

- **Password change kills Gmail-scoped tokens specifically.** A refresh token stops working
  when *"the user changed passwords and the refresh token contains Gmail scopes."* This is
  the **opposite** of Microsoft's confidential-client behavior, where a user password change
  leaves the token alive. A user changing their password breaks the Gmail connector.
- **The 100-token silent eviction.** *"There is currently a limit of **100 refresh tokens
  per Google Account per OAuth 2.0 client ID**. If the limit is reached, creating a new
  refresh token **automatically invalidates the oldest refresh token without warning**."*
  A connector that re-runs consent on every deploy will eventually knock out its own live
  token with no error. (Note: the figure sometimes quoted as 50 is wrong; the doc says 100.)
- **Six-month inactivity** expiry — not a risk for a polling connector.

Admin-side kill switch, and this is the likely cause of an overnight outage in a
locked-down firm:

> "If you change access to **Restricted**, any previously installed apps that you haven't
> trusted **stop working, and tokens are revoked**."
> — [Control which third-party apps access Workspace data](https://knowledge.workspace.google.com/admin/apps/control-which-third-party-and-internal-apps-access-google-workspace-data)

Gmail scopes are restricted scopes, so onboarding must include getting the client ID marked
**Trusted** in the customer's Admin console.

**UNVERIFIED:** token fate when an app is blocked via the *unconfigured-apps domain
default* rather than the Restricted-service path; the vendor page does not specify.

**Fix:** unfixable in code — but the connector needs **re-consent alerting**, not just
retry. Both platforms have documented paths where the token dies with no warning to the app.

---

## 13. Archive moves take mail out of Graph's reach entirely

`PLATFORM-FACT` · Buyers 1, 2

**What we did not ask:** where mail goes when the firm's archive policy fires.

> "The API does *not* support accessing in-place archive mailboxes, not on Exchange Online
> nor on Exchange Server."
> — [mail API overview](https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview?view=graph-rest-1.0)

And the immutable ID *"changes if: The user moves the item to an archive mailbox"*
([immutable IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)).
Auto-expanding archiving additionally creates subfolders named
`<folder name>_yyyy (Created on mmm dd, yyyy h_mm)` and redistributes items
([auto-expanding archiving](https://learn.microsoft.com/en-us/purview/autoexpanding-archiving)).

So a message the connector tracked can vanish from its addressable surface, with a changed
ID, because of a retention tag — and per finding 1, the connector's own move can be what
selects the DPT that triggers it. Law firms archive aggressively, which makes this a
first-order constraint rather than an edge case.

**Fix:** disclose. This one is unfixable; the mitigation is finding 1's — prefer
categorize-in-place.

---

## 14. The multi-tenant publisher decision is asymmetric, and Google charges annually

`SALES-QUESTION` · Buyer 1 · **This is the pending operator decision.**

**Path A — one rTech multi-tenant app in every customer's token chain.**

*Microsoft:* publisher verification costs **$0** — *"Microsoft doesn't charge developers
for publisher verification"* — but requires a verified Cloud Partner Program account, a
Partner Global Account, and a publisher domain that *"can't be `*.onmicrosoft.com`"*
([publisher verification](https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview)).
Without it, the consent prompt shows the app as *"unverified"* with no publisher
information ([configure publisher domain](https://learn.microsoft.com/en-us/entra/identity-platform/howto-configure-publisher-domain)),
and — the part that actually bites — risk-based step-up consent is **on by default**:
*"consent requests for newly registered multitenant apps that aren't publisher verified and
require nonbasic permissions are considered risky… **This step-up capability is enabled by
default**"* ([risk-based step-up consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-risk-based-step-up-consent)).
`Mail.Read` is nonbasic. So an unverified multi-tenant app cannot self-serve.

*Google:* restricted scopes require a **CASA third-party security assessment**, re-run
*"at least every 12 months"*
([restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)).
**Google publishes no price. UNVERIFIED — get a written quote from an approved assessor;
this is the only material unpublished number in the decision.**

*Obligations rTech takes on:* the [Identity Platform Terms of Use](https://learn.microsoft.com/en-us/legal/microsoft-identity-platform/terms-of-use)
make all activity under rTech's credentials rTech's responsibility (§3.2), require
indemnifying Microsoft (§7), and cap Microsoft's liability to rTech at **$500** (§6).
Google's [User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)
Limited Use prohibits **human reading** of user data except with explicit user agreement —
which constrains rTech staff from reading customer mail to debug.

*Blast radius:* a leaked secret reaches **every consented tenant at once**. Each customer
can revoke their own grant, but *"revoking the current granted permission doesn't stop
users from re-consenting"*
([manage application permissions](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/manage-application-permissions)).
**UNVERIFIED and important: no Microsoft doc describes a publisher-side global kill switch.**
rTech could not centrally contain a compromise.

**Path B — customer-owned single-tenant registrations.**

*Microsoft:* *"Member users can register applications"* by default — **Global Admin is not
required to create the registration**
([default user permissions](https://learn.microsoft.com/en-us/entra/fundamentals/users-default-permissions)),
though admin consent is still needed for Graph mail permissions. No verification, no CPP
account, no unverified badge, and single-tenant apps are exempt from the redirect-URI
root-domain restriction.

*Google:* verification is not needed when *"the app is only used by people in your Google
Workspace or Cloud Identity organization. The project must be owned by the organization,
and its OAuth Consent Screen must be configured for internal use"*
([when verification is not needed](https://support.google.com/cloud/answer/13464323)).
That also escapes the 100-user cap and the unverified-app screen. **CASA exemption is
INFERRED, not verbatim** — CASA sits inside verification, so no verification should mean no
CASA, but no vendor page says so in those words. Worth confirming in writing before betting
the delivery model on it.

**The decision in one line:** Path A buys a smooth consent click and pays with an annual
Google audit of unquoted cost, a contractual indemnity, and a cross-customer blast radius
rTech cannot centrally contain. Path B pushes per-customer setup work and removes rTech
from the token chain almost entirely. **Path B is the structurally safer default for a firm
this size, and the per-customer setup is exactly the repeatable workflow the
determinism-first rule says to freeze into a script.**

---

## 15. Gmail per-user quota caps throughput, and the code does not batch

`DOC-FIX` · Buyers 1, 3, 4

**What we did not ask:** what our actual per-mailbox throughput ceiling is.

Documented Gmail quota
([usage limits](https://developers.google.com/workspace/gmail/api/reference/quota)):
**6,000 quota units per minute per user per project**; 1,200,000/min per project;
80,000,000/day per project. Method costs: `messages.get` **20**, `messages.list` **5**,
`messages.modify` **5**, `history.list` **2**, `labels.list` **1**, `watch` **100**.

Note the doc expresses the per-user limit **per minute**, not per second — the widely
quoted "250 units per user per second" is **not** on the current page.

The connector's per-message path is `history.list` (2, amortized) + `messages.get` (20) +
`messages.modify` (5) ≈ **27 units per message**, and it does **not batch** — `gmail.py:337`
and `gmail.py:542` issue individual `list`/`get` calls, and no batch call exists in the
adapter. That yields roughly **222 messages per minute per mailbox** sustained, and
`messages.get` alone would cap at 300/min. Fine for steady-state triage; the constraint
appears on a cold start or a backlog.

**Graph, for the same question** ([throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits)):
**10,000 requests per 10 minutes per app+mailbox** and **4 concurrent requests**, and
Microsoft states *"Exceeding the limit for one mailbox doesn't affect the ability of the
application to access another mailbox."*

**On the MSP starvation question specifically:** a shared bucket **does** exist — the global
limit table's column header is literally *"Per app across all tenants"* at **130,000
requests per 10 seconds**, and Microsoft names *"a large number of requests from a particular
application across all tenants"* as a throttling cause
([throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)). But at 40
mailboxes each would need ~3,250 req/10s to reach it. **So: the shared bucket is real and
should be documented, but tenant 40 cannot realistically starve tenant 1 at this scale.**
This closes CAPABILITIES.md:359, which currently marks the per-mailbox Outlook figures
UNVERIFIED.

**No documented Graph daily cap** was found — the longest documented window is 10 minutes.
Absent, not proven nonexistent.

---

## 16. Ledger and logs never rotate

`CODE-FIX` · all buyers

**What we did not ask:** what an unattended connector does to the disk over a year.

`ledger.py:16-17` states the file is opened append-only and *"Rotation, if it is ever"*
needed, is deferred. `logging_config.py` contains no `RotatingFileHandler`, `maxBytes`, or
`backupCount`. For a product whose entire pitch is unattended operation, both grow without
bound. Low severity, trivially fixable, and embarrassing to discover on a client's machine.

---

## Areas checked and found genuinely fine

Stated so nobody re-investigates them:

- **Gmail change-feed resync is correct.** `gmail_history.py` treats Gmail's documented 404
  as normal control flow, returns `resync_required` rather than raising, and keeps the
  status code in exactly one module. Matches
  [the sync guide](https://developers.google.com/workspace/gmail/api/guides/sync)
  — history is *"typically available for at least one week"* and out-of-range returns 404.
- **The mid-page cursor rule is right.** `watcher.py:295-320` stores a cursor only on a
  final page, and `ChangeSet` refuses to carry both a cursor and a next-page token. This is
  the classic silent-data-loss bug and it is correctly prevented.
- **The replay window survives a resync** (`watchstate.py:386-388`) and `SEEN_LIMIT = 500`
  comfortably exceeds `BOOTSTRAP_LIMIT = 50`, so a cold start cannot overflow it.
- **What leaves the mailbox for classification is bounded.** `openai_compatible.py:381-383`
  sends sender, subject, and a snippet capped at 500 characters
  (`openai_compatible.py:247`); message bodies are never sent. For a law-firm
  confidentiality conversation this is the strongest thing the product has, and it is real.
- **Litigation hold does not block anything the connector does, and moving/categorizing does
  not disturb eDiscovery.** Copy-on-write for messages is triggered only by *Subject, Body,
  Attachments, Senders and recipients, Sent and received dates* — the table explicitly
  excludes *"Item location (when an item is moved between folders)"* and read/unread status
  ([Recoverable Items folder](https://learn.microsoft.com/en-us/exchange/security-and-compliance/recoverable-items-folder/recoverable-items-folder)).
  Google is the same shape: a trashed message under a Vault hold *"isn't purged"*
  ([place Gmail messages on hold](https://support.google.com/vault/answer/2473591)).
  **This is a sellable fact** — preservation sits *below* the client API on both platforms.
  Note it is orthogonal to finding 1: holds are fine, **retention** is not.
- **Push/webhook expiry is not a current risk.** There is no `users.watch` or Graph
  subscription code, so Gmail's 7-day watch renewal and Graph's 10,080-minute subscription
  lifetime do not apply to today's pull-only design. They will matter if push is added.
- **Google Data Regions cover Gmail content for processing, not just at rest** — *"data at
  rest (including backups) and data processing"*, with Gmail's row naming subject, body,
  senders/recipients, and attachments
  ([data covered by data regions](https://knowledge.workspace.google.com/admin/compliance/data-covered-by-data-regions)).
  Logs and cached content are excluded. **This is stronger than anything verifiable for
  Graph** (see below) and is worth stating to a residency-sensitive client — with the caveat
  that it governs Google's storage and processing, not our egress.

## Open UNVERIFIED items worth closing before a paid install

1. Whether a delegated `.Shared` token can **write** — move, draft, categorize — to a shared
   mailbox. Microsoft documents only GET. **Live-tenant test.**
2. Whether Graph can stamp a personal retention tag on a connector-created folder. No API
   surface found. **Determines whether finding 1 is fixable or only disclosable.**
3. Whether a **public-client** daemon is hard-blocked by a sign-in-frequency CA policy. Docs
   state the deferral only for confidential clients.
4. Google edition gating for domain-wide delegation and app access control — the admin pages
   carry no supported-editions banner. **Reseller confirmation, not more searching.**
5. Written confirmation that Internal-type Google apps are exempt from CASA. This inference
   is what makes Path B free.
6. A CASA quote from an approved assessor.
7. Whether Graph **processing** stays in-region for an EU tenant. Regional Graph endpoints
   are documented only for national clouds; `graph.microsoft.com` is a single global endpoint
   for commercial tenants. **Do not claim in-region processing to a client.** Also note
   Multi-Geo customers are *"not in scope for the EU Data Boundary"*
   ([Multi-Geo](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-multi-geo?view=o365-worldwide)).
8. Whether Graph mail APIs work against F3/Kiosk and resource mailboxes. Neither is
   documented either way.
