# Microsoft 365 Business Tenant Setup

This is the one path for a business Microsoft 365 tenant where admin consent
is required. Follow the six sections in order. Every click is named.

The connector takes the filing work off the mailbox owner's plate so the owner
spends the day on the mail that needs a person, not on sorting it. The owner
stays in charge of the mailbox; the connector files, and the ledger records
every action it took.

Live end-to-end on a business tenant was verified 2026-08-22 (issue #98
receipt). The steps below are that path.

Two people are involved:

- a **Global Administrator** of the tenant, for section 4
- the **mailbox owner**, for sections 5 and 6

## 1. Confirm the target mailbox has an Exchange Online licence

The connector reads and files mail through Microsoft Graph mail endpoints.
Those endpoints exist only for a mailbox hosted in Exchange Online. An account
without an Exchange Online service plan signs in successfully and then fails on
the first mail call, because there is no mailbox behind the token. Check the
licence first.

1. Open https://admin.microsoft.com in a browser and sign in as an
   administrator. The Microsoft 365 admin center home page loads, with a left
   navigation rail listing Home, Users, Teams & groups, Billing, and so on.
2. In the left navigation, click **Users**, then click **Active users**. A
   table of accounts loads with columns Display name, Username, and Licenses.
3. Click the display name of the mailbox owner you are installing for. A
   detail panel opens on the right with the user's name at the top and the
   tabs **Account**, **Devices**, **Licenses and apps**, and **Mail**.
4. Click the **Licenses and apps** tab. The panel shows a **Licenses** section
   with a checkbox per assigned plan, and below it an **Apps** section listing
   the service plans inside those licences.
5. Confirm a checked plan whose Apps list contains **Exchange Online**. Expand
   the **Apps** section to read the service plans if the licence name alone
   does not say it.

[screenshot: Licenses and apps tab with a checked Microsoft 365 licence and the Apps list expanded showing Exchange Online ticked]

If Exchange Online is not there, assign a licence that includes it and wait for
the mailbox to provision before continuing.

## 2. Sign in as the mailbox owner

Every Graph call the connector makes is against `/me/...`, the signed-in user's
own mailbox. There is no mailbox-selection setting and no `/users/{id}` path.

Shared mailboxes are out. A shared mailbox, a delegated mailbox, and a
colleague's mailbox cannot be targeted, even by an account holding full access
to them. Install against a licensed user account and sign in as the owner of
the mailbox being triaged.

If the mailbox you want triaged is a shared one, it needs a licensed user
account of its own. Raise that with rTech before installing.

## 3. There is nothing to register

The kit ships rTech's own Entra app registration. The recipient registers
nothing and creates nothing in Entra.

| Field | Value |
| --- | --- |
| Application (client) ID | `4be80172-cace-49cd-bbee-cef34841c8fb` |
| Display name | rTech Inbox Zero Connector |
| Supported account types | Multi-tenant, plus personal Microsoft accounts |
| Client type | Public client, no client secret exists |
| Redirect URI | `http://localhost` |
| Delegated Graph scopes | `Mail.ReadWrite`, `offline_access`, `User.Read` |

The registration lives in rTech's own paid tenant. It is the current identity
for this kit.

A client id is an identifier, not a secret, the same way a mobile app's bundle
id is. The registration is a public client with no secret, so the token that
MSAL mints is written to the mailbox owner's machine and to nothing else. rTech
never receives, stores, or is able to request that token.

## 4. Admin consent

A Global Administrator grants the three delegated scopes once, for the whole
organization.

1. Take the URL below and replace `<TENANT-ID-OR-DOMAIN>` with the tenant's
   verified domain, for example `contoso.com`, or its Directory (tenant) ID:

   ```
   https://login.microsoftonline.com/<TENANT-ID-OR-DOMAIN>/adminconsent?client_id=4be80172-cace-49cd-bbee-cef34841c8fb
   ```

2. Open that URL in a browser. Microsoft's sign-in page loads. Sign in with a
   **Global Administrator** account. Sign in with the admin account, not the
   mailbox owner's account.
3. A **Permissions requested** screen appears. It names **rTech Inbox Zero
   Connector**, shows the publisher line reading **unverified publisher**, and
   lists the three permissions being requested: read and write access to the
   user's mail, maintain access to data the user has given access to, and sign
   in and read the user's profile. The unverified-publisher line is expected on
   this registration and is not an error.

   [screenshot: Permissions requested consent screen naming rTech Inbox Zero Connector, the unverified publisher line, and the three listed permissions]

4. Check **Consent on behalf of your organization** and click **Accept**. The
   browser redirects to `http://localhost` and the page fails to load. That
   failed page load is the expected end of the flow; consent was recorded
   before the redirect.
5. Confirm the grant. Open https://entra.microsoft.com and sign in as the same
   administrator. The Microsoft Entra admin center loads with a left navigation
   rail.
6. In the left navigation, click **Identity**, then **Applications**, then
   **Enterprise applications**. A table of enterprise applications loads.
7. Click **rTech Inbox Zero Connector** in that table. The application's
   Overview page opens, showing Name, Application ID
   `4be80172-cace-49cd-bbee-cef34841c8fb`, and Object ID.
8. In the application's left menu, click **Security**, then click
   **Permissions**. The **Admin consent** tab lists the granted permissions.
9. Read the table. Three rows are present, each with API name **Microsoft
   Graph**, type **Delegated**, and **Granted for <organization name>**:
   `Mail.ReadWrite`, `offline_access`, `User.Read`.

   [screenshot: Enterprise applications Permissions page showing Mail.ReadWrite, offline_access and User.Read each granted for the organization]

`Mail.Send` is not in that list and is never requested. The mailbox owner can
now sign in without seeing a consent prompt of their own.

## 5. Install and first sign-in on the mailbox owner's machine

Run these on the mailbox owner's own desktop machine, signed in as the mailbox
owner. Commands are Windows PowerShell first, macOS and Linux beside.

1. Install `uv`.

   Windows (PowerShell):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   macOS/Linux:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   Open a NEW terminal afterwards so `uv` is on PATH, then run `uv --version`
   to confirm.

2. Change into the `connector` directory of the unzipped kit and install the
   locked dependency set.

   Windows (PowerShell):

   ```powershell
   cd C:\path\to\inbox-zero\connector
   uv sync
   ```

   macOS/Linux:

   ```bash
   cd /path/to/inbox-zero/connector
   uv sync
   ```

3. Initialize the taxonomy and config. Same commands on every OS:

   ```powershell
   uv run inbox-zero-connector init-taxonomy
   uv run inbox-zero-connector setup
   ```

   `setup` needs a real terminal. It mints this install's `install_id`, which
   namespaces the config directory, the data directory, and the keychain
   account. `auth` refuses until `setup` has run. Full detail on both commands
   is in [../INSTALL-RUNBOOK.md](../INSTALL-RUNBOOK.md) sections 3 and 6b; a
   pre-configured zip from rTech already carries these settings, so run the
   installer with no flags instead.

4. Sign in:

   ```powershell
   uv run inbox-zero-connector auth microsoft
   ```

   ```bash
   uv run inbox-zero-connector auth microsoft
   ```

   The default browser on this machine opens on Microsoft's sign-in page. Sign
   in as the mailbox owner. Because the administrator granted consent in
   section 4, the sign-in completes without a consent screen; the browser lands
   on a page saying authentication is complete and the tab can be closed.

   [screenshot: browser returned to the authentication-complete page after the mailbox owner signs in]

5. Back in the terminal, the command prints two lines:

   ```
   Authenticated as owner@yourdomain.com.
   Credential stored in the OS keychain. No token was written to disk.
   ```

   The address on the first line is the signed-in user's UPN. Confirm it is
   the mailbox you intend to triage.

The browser flow is the path. If the connector prints a device code and a URL
instead, stop. Entra security defaults refuse the device-code flow with
`AADSTS530035`. Run `auth microsoft` again from a desktop session that can open
a browser.

Tokens land in the OS credential store through `keyring`: Windows Credential
Manager, macOS Keychain. No token is written to a file.

## 6. First watch round

1. Set the provider and run one pass.

   Windows (PowerShell):

   ```powershell
   $env:INBOX_ZERO_PROVIDER = "microsoft"
   uv run inbox-zero-connector watch --once
   ```

   macOS/Linux:

   ```bash
   INBOX_ZERO_PROVIDER=microsoft uv run inbox-zero-connector watch --once
   ```

2. The pass polls the mailbox once and exits. It logs one counts-only round
   record of this shape:

   ```
   WATCH LOOP: round complete streams_polled=1 errors=0 resyncs=0 seen=2 skipped_out_of_scope=0 skipped_already_seen=0 classified=2 applied=2 failed=0 cursors_stored=1
   ```

   A correct round has `errors=0` and `failed=0`, and `applied` equal to the
   number of messages that were filed. On the 2026-08-22 live tenant round,
   two messages were filed through Graph
   (`POST /me/messages/<id>/move`).

3. Check the exit code. `watch --once` exits **0** when the pass was clean and
   **1** when it recorded any error. A failed round still prints a
   normal-looking summary, so read the code, not the text. In PowerShell that
   is `$LASTEXITCODE`; in a shell, `echo $?`.

4. Find the ledger. Run:

   ```powershell
   uv run inbox-zero-connector paths
   ```

   It prints seven labelled lines. The last one is the ledger:

   ```
   install id: <id>
   config dir: <path>
   taxonomy:   <path>
   config:     <path>
   secrets:    <path>
   data dir:   <path>
   ledger:     <path>
   ```

   The path printed on the `ledger:` line is the append-only audit ledger for
   this install, inside the per-user data directory named on the `data dir:`
   line. Trust that command for the location on this machine.

5. Confirm the ledger grew by the number of messages the round filed, then
   read the digest:

   ```powershell
   uv run inbox-zero-connector digest
   ```

   It prints today's audit digest, and the counts in it match the round. On
   the 2026-08-22 live tenant round the ledger went from 10 rows to 12 and the
   digest matched.

The install is working. Section 7 onward of
[../INSTALL-RUNBOOK.md](../INSTALL-RUNBOOK.md) covers the continuous watch
loop, chat-app registration, and diagnostics.

The chat path is Claude Desktop OR Codex in the ChatGPT app. Register Claude
Desktop with section 8 of [../INSTALL-RUNBOOK.md](../INSTALL-RUNBOOK.md), and
Codex with section 8b. In the ChatGPT app, MCP servers are visible ONLY in the
Codex tab; a normal ChatGPT chat cannot see or use them and will say the tool
is unavailable.
