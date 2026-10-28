# Inbox Zero Connector — Stranger Install Runbook

Status: the connector's code is RUNNING (live-mailbox receipts below); THIS
RUNBOOK is WRITTEN, not yet stranger-tested. [README.md](README.md) states the
same three words for the same artifact. This runbook is the #98
done-means-2 artifact; it closes only when someone who has never seen this
repo follows it on an unseen machine and ends with a working deploy. Live
Microsoft 365 end-to-end was verified 2026-08-22 on a live tenant (issue #98
receipt). Gmail end-to-end was live-verified 2026-08-18.

Commands are given Windows PowerShell first, with the macOS/Linux
equivalent beside each. Every connector verb named here exists in
`uv run inbox-zero-connector --help`. Background and rationale live in
[README.md](README.md); this file is only the ordered steps.

**Fast path.** Steps 1–3 and 6 are one idempotent script — `.\install.ps1
-OwnerDomain yourdomain.com` (Windows) or `./install.sh --owner-domain
yourdomain.com` (macOS/Linux) — safe to re-run, ends with a verify pass.
The numbered steps below remain the manual equivalent and the reference
for what the script does. **Using an AI assistant** (Claude Desktop,
Claude Code, ChatGPT/Codex)? Add this folder to it — [AGENTS.md](AGENTS.md)
tells it exactly how to drive this install for you. Steps 4 and 5 are
identity steps and always involve a human.

**Pre-configured zip.** If rTech built this zip for you, it already carries
your settings: `config/connector-config.json` and `secrets/config.json` are
filled in. Skip step 3 entirely — do not pass `-OwnerDomain`, because the
domain is already set — and step 4 was done for you with your organization's
admin. Run the installer with no flags, then go straight to steps 5–7. The
installer writes `logs/install-<UTC>.log`; send that file back with your
diagnostics.

## 1. Prerequisites

You need [uv](https://docs.astral.sh/uv/). It manages Python for you —
this project pins Python 3.14 (`.python-version`, `requires-python =
">=3.14"` in `pyproject.toml`) and `uv sync` fetches it if missing.

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS/Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a NEW terminal afterwards so `uv` is on PATH, then confirm:
`uv --version`.

## 2. Get the code and install

Copy or clone the kit, then work from the `connector` directory. All
remaining commands in this runbook run from there.

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

`uv sync` creates `.venv` and installs the locked dependency set. For
environments without uv, `requirements.txt` is exported from `uv.lock`
(see [README.md § Install](README.md#install)).

### Upgrading in place — history, first written for 0.2.7 → 0.2.8

This section is kept for the shape of an upgrade, not for those two version
numbers; the current build is later than either. For the supported update path
today, read **§ 9b Updating** below, which starts from `update-check`.

An upgrade replaces the CODE only. Your settings, taxonomy, credentials, and
audit history are not inside the unzipped folder — `paths` shows where they
actually live, and on this machine every one of them is outside it:

- config, taxonomy, and `secrets.json` are in the per-user config directory
  (`platformdirs`; on macOS this is under
  `~/Library/Application Support/inbox-zero-connector/`)
- the ledger is in the per-user data directory
- OAuth tokens are in the OS keychain, never in a file

So the upgrade is: unzip 0.2.8 into a **new** folder, run `uv sync` there, and
run from it. Do not unzip over the 0.2.7 folder — a stale `.venv` and removed
files survive an overwrite and you get a mix of two versions.

```powershell
uv sync
uv run inbox-zero-connector paths        # same config/ledger paths as before
uv run inbox-zero-connector show-scopes  # posture unchanged by the upgrade
uv run inbox-zero-connector watch --once
```

You should NOT need to re-run `setup`, re-enter credentials, or sign in again:
`auth` reads the existing keychain entry. If `show-scopes` reports a scope set
you did not expect, or `auth` prompts for consent again, stop and report it —
that means the shipped app identity or scope set changed under you, which 0.2.8
does not intend for an existing Microsoft install (0.2.8 changed the Microsoft
authority default to `common` and ships rTech's app id; a 0.2.7 install that
already overrode `microsoft_client_id` keeps its own).

Once 0.2.8 is verified, the old folder can be deleted. Keep its `logs/`
directory if rTech has asked for install transcripts.

## 3. Initialize taxonomy and config

Same commands on every OS (uv normalizes the shell). The interactive
wizard is the primary path:

```powershell
uv run inbox-zero-connector init-taxonomy
uv run inbox-zero-connector setup
uv run inbox-zero-connector paths
```

`setup` needs a real terminal (it refuses to run without a TTY). The
non-interactive equivalent, for scripted or headless installs, is:

```powershell
uv run inbox-zero-connector init-config --owner-domain YOURDOMAIN.com
```

- `init-taxonomy` writes the default folder set. The vocabulary is
  closed: the connector rejects folders not listed in it.
- `setup` prompts for the drafts posture, voice routing, classifier, and
  the credential IDENTIFIERS (`google_client_id`, `microsoft_client_id`,
  `microsoft_tenant_id`), writes `connector-config.json`, and creates a
  secrets skeleton for you to fill in by hand — see step 4. It also MINTS
  this install's `install_id` if it does not already have one, and prints it
  in the summary. That id is what namespaces the OS keychain account, so
  `auth` in step 5 refuses until this step has run — see § 6b.
- `init-config` writes the same drafts posture and voice routing table
  without prompting, and mints the same `install_id` the same way.
  `--owner-domain` is required (it defines internal vs external mail).
  **Drafts are OFF by default on both providers** — the flags that turn
  them on, and what each costs, are documented in
  [README.md § Configure drafts](README.md#configure-drafts-and-the-voice-routing-table).
  Do not enable drafts during a first install; prove triage first.
- `paths` prints where the config, taxonomy, and audit ledger live on
  THIS machine (locations are per-OS; trust the command, not this doc).

**The wizard asks for the whole account list before anything signs in.** For
each mailbox it takes the address, infers the provider from the domain (asking
when a custom domain does not state it), and takes that mailbox's own owner
domain, posture, and drafts answer — posture and drafts are per account, the
taxonomy is shared. **`watch` polls every account in that list once per round**,
applying each mailbox's own posture and drafts answer, writing one ledger whose
rows name the mailbox — with one limit, and it is the keychain's: an install
stores one credential per provider, so two mailboxes on the **same** provider
still need two installs (§ 6b).

### Pick your tier

The same install runs both tiers; the tier is the config. Run exactly one:

```bash
examples/tier-basic.sh --owner-domain YOURDOMAIN.com   # folder triage only
examples/tier-ai.sh    --owner-domain YOURDOMAIN.com   # triage + voice + reply drafts
```

- **Basic**: posture `file_only`, drafts off everywhere. Mail is filed into
  the taxonomy folders; nothing is composed. On **Microsoft** that is also a
  scope-layer no-send guarantee — `Mail.ReadWrite` is refused by Graph's send
  paths. On **Gmail it is not**: the shipped interactive consent requests bare
  `gmail.modify`, which Gmail's send methods also accept, so drafts-off Gmail
  is a **code-layer** guarantee (this connector has no send code path) rather
  than a scope-layer one. Turning drafts off does not change the Gmail scope
  set. See
  [README.md § The no-send claim](README.md#the-no-send-claim-stated-exactly).
- **AI**: posture `file_read_draft`, Microsoft drafts on (no extra scope, still
  scope-layer). `--google` also enables Gmail drafts; on the interactive flow
  that requests no additional scope, because `gmail.modify` already covers
  draft creation. The example script's `--google` is a shorthand: it passes the
  CLI's own `--enable-google-drafts` flag to `init-config` (the spelling used
  above and in `AGENTS.md`), so the two names are one setting.
  The voice profile is built after sign-in from your own sent mail, inside
  Claude Desktop.
- Windows: `.\examples\tier-basic.ps1` / `.\examples\tier-ai.ps1`
  (`-OwnerDomain`, `-Force`, `-Google`), same result.
- Classifier endpoint: both scripts take `--classifier-base-url URL` and
  `--classifier-model ID` (`-ClassifierBaseUrl` / `-ClassifierModel` on
  Windows). Exactly three fields per message go to that URL: sender,
  subject, and the first 500 characters of the preview snippet -- never the
  body (see `docs/what-leaves-the-machine.md`). The endpoint is the client's
  own; rTech-hosted classification is by request only, paid, under NDA. The
  API key goes into `secrets.json`, never on a flag.
- Switch later with `--force` on the other script.

## 4. Microsoft 365 — consent to the rTech app

### Business tenants: follow docs/m365-tenant-setup.md

A business Microsoft 365 tenant requires admin consent, an Exchange Online
licence check, and a Global Administrator. That whole path, every click named,
is [docs/m365-tenant-setup.md](docs/m365-tenant-setup.md). Follow it, then come
back to step 5.

Common to every install:

**The mailbox needs an Exchange Online licence.** The connector reads and files
mail through Microsoft Graph's mail endpoints, which exist only for a mailbox
hosted in Exchange Online. An account with no Exchange Online service plan can
complete sign-in and consent and then fail on the first mail call, because there
is no mailbox behind the token.

**Shared mailboxes are not supported.** Every Graph call this connector makes
is against `/me/...` — the signed-in user's own mailbox
(`src/inbox_zero_connector/providers/outlook/provider.py:30-32`). There is no
`/users/{id}` path and no mailbox-selection setting. Sign in as the mailbox
OWNER. A shared mailbox needs a licensed user account of its own; raise it with
rTech before installing.

**There is nothing to register.** The kit ships rTech's own Entra app
registration, and it is a MULTI-TENANT PUBLIC CLIENT: no client secret exists
for it, the consent screen you see is your own tenant's, and the token MSAL
mints is written to THIS machine's credential store. rTech is never in the
token chain — it never receives, stores, or can request your token. What it
ships is an application IDENTIFIER, public by construction the same way a
mobile app's bundle id is.

The application id is already in `connector-config.json`
(`credentials.microsoft_client_id` =
`4be80172-cace-49cd-bbee-cef34841c8fb`), display name **rTech Inbox Zero
Connector**. No tenant id is set, so the connector signs in against the
`common` authority. The delegated Graph scopes are `Mail.ReadWrite`,
`offline_access`, and `User.Read`. `Mail.Send` is never requested — verify on
your install with `show-scopes` in step 6.

Then go to step 5 and sign in as the mailbox owner.

### Advanced: bring your own registration

Optional, and only if your policy requires the registration to live inside
your own tenant. It changes nothing about how the connector behaves.

Portal steps ([Entra admin center](https://entra.microsoft.com) →
App registrations → New registration):

1. **Name:** anything, e.g. `inbox-zero-connector`.
2. **Supported account types:** "Accounts in this organizational
   directory only" (single tenant) is fine — note your Directory
   (tenant) ID if you choose it.
3. **Redirect URI:** platform **Public client/native (mobile & desktop)**,
   value `http://localhost`. The connector signs in with the browser
   (authorization-code + PKCE) flow on a loopback redirect
   (`src/inbox_zero_connector/providers/outlook/consent.py`). This is
   REQUIRED: Entra security defaults, on by default in every tenant, block
   the device-code flow (AADSTS530035), so a registration without this
   redirect cannot sign in at all.
4. After creation, open **Authentication** → set **Allow public client
   flows** to **Yes** (keeps the device-code fallback available for a
   headless install; a default-secured tenant refuses it regardless).
5. Open **API permissions** → Add a permission → Microsoft Graph →
   **Delegated** → add **`Mail.ReadWrite`** — and nothing else.
6. Grant admin consent if your tenant requires it for `Mail.ReadWrite`.

Copy the **Application (client) ID** — and the **Directory (tenant)
ID** if you registered single-tenant — into the config, OVERRIDING the
shipped rTech values. If you ran `setup` in step 3, you were prompted for
both; re-run `setup` to change them.

These are IDENTIFIERS, not secrets. The secret VALUES —
`google_client_secret` and `classifier_api_key` — are never asked for at
a prompt: `setup` creates a `secrets.json` skeleton in the config
directory and you type them in there yourself, in your own editor. Run
`uv run inbox-zero-connector paths` for the exact location on this
machine.

Environment variables remain available as the alternative, and they WIN
over the config file on conflict (`config.py`, `CredentialSettings`;
tenant defaults to `common`, the authority built at
`src/inbox_zero_connector/services/auth/keychain.py`):

Windows (PowerShell):

```powershell
$env:INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID = "<application-client-id>"
$env:INBOX_ZERO_CREDENTIALS__MICROSOFT_TENANT_ID = "<directory-tenant-id>"  # only if single-tenant
```

macOS/Linux:

```bash
export INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID="<application-client-id>"
export INBOX_ZERO_CREDENTIALS__MICROSOFT_TENANT_ID="<directory-tenant-id>"  # only if single-tenant
```

For the Gmail-side registration (Google Cloud OAuth client, Desktop
app), follow
[README.md § Register an OAuth client](README.md#register-an-oauth-client-in-your-own-tenant) —
including the Workspace domain-wide-delegation caveat there.

## 5. Authenticate

Microsoft first:

```powershell
uv run inbox-zero-connector auth microsoft
```

This opens the default browser on the machine running the command. **The
browser flow is the supported path.** Sign in as the mailbox owner and approve
the `Mail.ReadWrite` consent.

If no browser can be opened (a headless or remote-shell install), the connector
falls back to printing a device code and a URL. **Treat that fallback as a
failure signal, not a normal path:** Entra security defaults are on by default
in every tenant and refuse the device-code flow with `AADSTS530035`. If you see
a device code, run the sign-in from a desktop session with a browser instead of
working around it.

Tokens land in the OS credential store via `keyring` (Windows Credential
Manager / macOS Keychain) — never in a file.

Gmail second (if deploying Google):

```powershell
uv run inbox-zero-connector auth google
```

Google shows a "Google hasn't verified this app" screen for the shipped
client. Expected, not an error: click **Advanced**, then
**Go to ... (unsafe)**, then **Continue**. Only listed test users get past
it, and in Testing the grant expires after 7 days. Details:
`docs/what-leaves-the-machine.md`.

Remove a stored credential any time with
`uv run inbox-zero-connector logout microsoft` (or `google`). The provider
argument is required — a bare `logout` is refused. It asks to confirm; add
`--yes` to skip that prompt, which is **required** when stdin is not a
terminal, so any scripted or agent-driven run needs it or it hangs.

## 6. Verify the scope posture

```powershell
uv run inbox-zero-connector show-scopes
```

This prints the LIVE manifest computed from your installed config —
usable as evidence in a security questionnaire. On a default (drafts
off) install, confirm: `microsoft:` requests exactly `Mail.ReadWrite`
and lists `Mail.Send` as NOT requested.

## 6b. One credential per provider per install

`watch` polls **every account in the configured list** each round, so one
install serves several mailboxes — a Gmail account and a Microsoft 365 account
together, each with its own posture and its own drafts answer, filing into the
one taxonomy this install agreed to. The ledger is one file and each row names
the mailbox it belongs to.

**The limit is the keychain, and it is a hard one: one stored credential per
PROVIDER per install.** Every install has an `install_id` that namespaces its
config directory, its data directory (ledger and logs), and its stored
credential; within that namespace the credential is keyed by provider name
alone. So two Gmail mailboxes on one install would resolve the same token and
both poll whichever one signed in last — one mailbox watched twice, the other
not at all. `watch` refuses to start such a config rather than run it, and
names the fix: give the second mailbox its own install with its own
`install_id` (below). Two mailboxes on *different* providers have different
keychain accounts and run together correctly.

**Every install has one — it is never blank.** A custom zip carries the id
rTech stamped into it at build time. A self-installed copy has none until step
3, and the first `setup` (or `init-config`) run MINTS one and writes it into
`config/connector-config.json` inside the install folder. It is minted once and
never re-minted: re-running the installer keeps the id you already have,
because changing it would move the config directory, the data directory, and
the keychain account all at once and orphan the taxonomy, the ledger, and the
stored token.

```powershell
uv run inbox-zero-connector paths
```

The first line is `install id:` — the id rTech stamped, or the one your first
`setup` minted — and every path below it sits inside that namespace.

An install with no id cannot authenticate at all. Commands that reach the OS
keychain refuse with a configuration error naming `setup`, rather than falling
back to an un-namespaced credential account that any other install on the
machine may already own. If you see that refusal, you have not run step 3 yet.

Running two mailboxes **on the same provider** means TWO install folders with
DIFFERENT install ids. (Two mailboxes on different providers need only the one
install — list both accounts and `watch` polls each.) Unzipping the same custom
zip twice does not give you two
installs: both copies carry the SAME stamped id, so they resolve the same
directories and the same keychain account, and the second `auth` overwrites the
first mailbox's token — mail then files into the wrong mailbox's folders. If you
need a second mailbox, ask rTech for a second zip. (Two self-installed copies
each mint their own id, so they are isolated; setting
`INBOX_ZERO_INSTALL_ID` names an install explicitly and wins over the file.)

If you name the install with `INBOX_ZERO_INSTALL_ID`, that variable must be
present on **every** invocation — including launchd plists, cron entries, and
systemd or Windows service units — because `init-config` reads it but does not
persist it, so the interactive install works while a scheduled job that omits it
does not.

**Know what that failure looks like, because it does not name the variable.**
The install id namespaces the config and data directories, so dropping it does
not produce an "uninitialized install" refusal — it silently resolves the
**un-namespaced** directory instead, which is empty. The job therefore fails
earlier than the keychain, with a refusal about whatever it looks for first:

```
Taxonomy error: No taxonomy installed at <un-namespaced path>/taxonomy.json.
Run 'uv run inbox-zero-connector init-taxonomy' ...
```

(or a `Configuration error` naming the same wrong directory). Both exit 1 and
both point at a directory that is **not** your install.

**Do not run `init-taxonomy` there.** Following that message writes a second,
empty install beside the real one and leaves the scheduled job reading it —
you get a connector that starts and files nothing into the mailbox you set up.
The fix is always to restore `INBOX_ZERO_INSTALL_ID` in the job definition.
Compare the path in the error against `uv run inbox-zero-connector paths` run
**with** the variable set: if they differ, the variable is what is missing.

Only commands that reach the keychain — `auth`, `logout` — get the
uninitialized-install refusal that names the id directly.

### One watcher per mailbox — across all machines

A mailbox may be watched by exactly ONE running `watch` loop anywhere. Two
watchers on the same mailbox — a laptop and a desktop, or an old install nobody
uninstalled — race the same messages: both classify a message and label it, both
move it (the later move wins, so the message lands wherever the slower machine
decided), and each writes its own ledger, so you end up with two audit trails
that each look authoritative and neither of which is the whole story. Nothing
detects this; there is no lease and no cross-machine lock, so **the constraint is
operational and it is yours to hold.**

The trap is the install you forgot: a machine you set up months ago, still
logged in, still running `watch` on a schedule, quietly fighting the new one. If
triage results look inconsistent — messages moving after you filed them, a
digest that disagrees with the mailbox — look for a second watcher before
looking for a bug. The fix is to stop it: on the losing machine remove the
scheduled job (launchd plist, cron entry, systemd unit, or Windows scheduled
task), stop the running loop, and run `uv run inbox-zero-connector logout
google|microsoft` so it cannot resume. Two DIFFERENT mailboxes on one machine is
fine and is what 6b above is about; the same mailbox in two places is not.

## 7. Run the watch loop

Preview first. The first real pass CREATES the taxonomy's folders in the
mailbox, and the dry run is the only chance to see which ones before they
appear in the owner's mail client:

```powershell
uv run inbox-zero-connector watch --once --dry-run
```

It classifies exactly as a real pass would, files nothing, writes no receipt,
and does not move the stream position -- so the real pass below sees the same
mail. `--dry-run` REQUIRES `--once`: there is no dry continuous loop, and
`watch --dry-run` on its own is refused. Read the `WOULD CREATE` list with the
owner and confirm those folders are wanted before continuing.

If a pass has already filed mail the owner disagrees with, `uv run
inbox-zero-connector undo` prints every message it moved, the folder it came
from, and the folders the run created. It reads the ledger's own receipts, so
it never guesses a source folder. It PREVIEWS only — nothing moves — unless
you add `--apply`, which performs the reversal and receipts every move:

```powershell
uv run inbox-zero-connector undo                 # preview the most recent run
uv run inbox-zero-connector undo --run ID        # preview one run by ledger id
uv run inbox-zero-connector undo --last --apply  # actually move the mail back
```

`--last` is the default and reverses the most recent run that filed anything;
`--run ID` picks one specific run by the run id shown in the ledger. Read the
preview with the owner before adding `--apply`. Folders the run created are
listed for the owner to judge and are never deleted.

```powershell
uv run inbox-zero-connector watch --once   # one polling pass, then exit
uv run inbox-zero-connector watch          # continuous; --interval N to tune
```

`watch --once` reports the round in its exit code: **0** when the pass was
clean, **1** when it recorded any error -- an expired or missing credential, or
a provider outage. Check it in any wrapper or scheduled task, because a failed
round still prints a normal-looking round summary. In PowerShell that is
`$LASTEXITCODE`; in a shell, `echo $?`. The continuous `watch` survives errors
by design and exits 0 whenever it stops cleanly.

## 7b. Run it as a service

A foreground `watch` stops when the terminal closes, when the owner logs out,
and at every reboot -- and nothing announces that it stopped. The mailbox just
quietly stops being filed, and the first evidence is the backlog days later.
Hand the loop to the OS supervisor instead.

**The installer now does this for you.** `install.sh` and `install.ps1` end by
registering the service for the host platform, idempotently, and print what was
registered and how to stop it. The steps below are the manual path -- for a
re-register, a second mailbox, or an install whose registration was skipped
(the installer skips it, and says so, when the shipped config names no
provider). Registration happens BEFORE sign-in, so the service fails and
retries until step 5 is done; that is expected and the restart policy is
spaced for it.

The connector prints the unit for THIS install, with the absolute `uv` path,
working directory, and log paths already resolved. The `service-unit` command
itself **prints only** -- it installs nothing and writes no file. You (or the
installer) place it.

```powershell
uv run inbox-zero-connector service-unit google --platform launchd    # macOS
uv run inbox-zero-connector service-unit google --platform systemd    # Linux
uv run inbox-zero-connector service-unit google --platform schtasks   # Windows
```

Substitute `microsoft` for a Microsoft mailbox. The label and filenames carry
the provider, so a machine watching two different mailboxes (step 6b) gets two
independently loadable services.

### macOS (launchd)

Save the printed plist to the path the command names, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rtech.inbox-zero-connector.google.plist
launchctl print gui/$(id -u)/com.rtech.inbox-zero-connector.google   # confirm it is loaded
```

To stop and unload it:

```bash
launchctl bootout gui/$(id -u)/com.rtech.inbox-zero-connector.google
```

Output lands in the connector's own log directory (the plist names both files;
`uv run inbox-zero-connector paths` reports the directory).

### Linux (systemd --user)

Save the printed unit to the path the command names, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now inbox-zero-connector-google.service
systemctl --user status inbox-zero-connector-google.service
journalctl --user -u inbox-zero-connector-google.service -f    # follow the log
```

To stop it:

```bash
systemctl --user disable --now inbox-zero-connector-google.service
```

A `--user` unit stops when the owner's login session ends unless lingering is
enabled for that account (`sudo loginctl enable-linger $USER`). On a laptop
that is usually what you want; on an always-on box, enable lingering.

### Windows

`--platform schtasks` prints a PowerShell `Register-ScheduledTask` script with
the absolute `uv.exe` path, working directory, and log paths already resolved
-- a scheduled task does not inherit your interactive shell's environment, so
resolving them at print time avoids the same trap the launchd and systemd units
avoid. Save the printed script and run it:

```powershell
uv run inbox-zero-connector service-unit google --platform schtasks > register-google.ps1
.\register-google.ps1
Get-ScheduledTask -TaskName 'com.rtech.inbox-zero-connector.google'   # confirm
```

The task runs **at logon** as the interactive user (not SYSTEM -- the watch
needs the mailbox owner's credential store) and restarts on failure on the same
30-second spacing as the systemd unit. `-Force` is what makes re-running it
safe: it replaces a task of the same name instead of failing on a duplicate.

To stop it:

```powershell
Unregister-ScheduledTask -TaskName 'com.rtech.inbox-zero-connector.google' -Confirm:$false
```

Output lands in the connector's own log directory, the same as launchd -- the
script redirects both streams there, because a scheduled task's console output
otherwise goes nowhere.

### Restart policy

All three restart the watch when it **fails** and leave it alone when it exits
**cleanly**, so an operator who stops the service on purpose does not fight the
supervisor: launchd uses `KeepAlive` with `SuccessfulExit = false`, systemd uses
`Restart=on-failure` with a 30-second `RestartSec`, and Task Scheduler uses
`-RestartInterval` on the same 30 seconds (long enough that a provider
outage is not a restart storm against their API, short enough that a transient
failure heals before the next mail arrives).

### Offboarding

**Tearing the service down is part of removing an install.** Run the `bootout`,
`disable --now`, or `Unregister-ScheduledTask` line above BEFORE the uninstall, or a
supervisor keeps restarting a connector whose credentials are gone -- and, worse
on a shared mailbox, an old machine's service is exactly the second watcher the
one-watcher-per-mailbox rule forbids.

`uv run inbox-zero-connector uninstall google|microsoft` walks the rest. It is a
**guided** offboarder: it deletes the stored credential itself, then prints the
teardown, revocation, and file-removal steps it deliberately leaves to you — the
`bootout`/`disable --now` lines above, the provider's own revocation page (this
connector cannot revoke server-side access; the grant lives in the owner's
account), and the `rm` lines for the local config and data directories, which it
never runs. Labels and filed mail stay in the mailbox by design.

Like `logout`, `uninstall` confirms before it acts. Add `--yes` to skip the
prompt; it is **required** when stdin is not a terminal, so a scripted offboard
without it hangs waiting for an answer nobody can type.

## 7c. Learned rules and the voice profile

### Learned rules — reading and turning one off

When the owner corrects a filing (the `record_correction` MCP tool: "this
belongs in Clients, not Vendors"), the connector both refiles the message and
LEARNS a rule from it. Rules are written by owner corrections only — never by
hand and never by the model — and this verb reads the table and turns rows off;
it cannot create one.

```powershell
uv run inbox-zero-connector rules list
uv run inbox-zero-connector rules disable RULE_ID --reason "client moved teams"
```

`rules list` prints every rule, enabled and disabled alike. `rules disable` KEEPS
the rule and its reason and turns it off, so the record of why survives. If a
correction produced a rule that over-fires, disabling it is the fix; there is no
delete. (`rules --list` and `rules --disable ID` are accepted as the same thing.)

### Voice profiles — build, read, approve

A voice profile describes how the owner WRITES, as patterns and never as quoted
text. It is only needed on the AI tier (`file_read_draft` posture with drafts
on). **It is unusable until the owner reads it and approves it** — `voice
approve` is the only thing that raises that flag, and a profile that has not
been approved is not used for drafting.

```powershell
uv run inbox-zero-connector voice build OWNER --sent-label "Sent Mail" --sent-after 2026-01-01
uv run inbox-zero-connector voice show OWNER      # the owner reads it in full
uv run inbox-zero-connector voice approve OWNER   # only now is it usable
```

The voice name is the one configured in `voice_routing.voices`. `build` stores
the profile UNAPPROVED, always — the two steps cannot be collapsed.

`build` takes its material from one of two sources:

- **Sent mail**, selected explicitly by `--sent-label LABEL` (repeatable),
  `--sent-after YYYY-MM-DD` (inclusive), and `--sent-before YYYY-MM-DD`
  (exclusive). `--sent-limit N` caps how many messages are read but is **not a
  selection on its own**: a limit with no label or date bound is refused rather
  than read as "the whole folder." That refusal is the design — sent mail is the
  most sensitive thing this connector reads, so the owner names the slice.
- **An external corpus**, `--from-corpus DIR`, a directory holding a
  `profile.md` describing how the owner writes. Use this when the owner would
  rather hand over prepared writing than have sent mail read at all.

## 8. Claude Desktop registration

The connector is a local stdio MCP server (`serve` is the default
verb). It prints its own config block with the absolute path already
resolved:

```powershell
uv run inbox-zero-connector desktop-config microsoft   # or google
```

Merge that block into `claude_desktop_config.json` (the command prints
where the file lives on your OS), fill the placeholder env values from
step 4, and restart Claude Desktop. Background:
[README.md § Claude Desktop configuration](README.md#claude-desktop-configuration). Before an org rollout, read
the Team/Enterprise gate in that same README section: an org
desktop-extension allowlist or MDM policy can block local MCP servers
entirely.

## 8b. Codex (ChatGPT app) registration

The connector also runs as a local MCP server for Codex. Codex lives inside
the ChatGPT desktop app as its own tab, and also as the `codex` CLI. **In the
ChatGPT app, MCP servers are visible ONLY in the Codex tab; a normal ChatGPT
chat cannot see or use them and will say the tool is unavailable.**

1. Open `~/.codex/config.toml` (create the file if it does not exist) and add
   this block, replacing the two placeholders:

   ```toml
   [mcp_servers.inbox-zero]
   command = "uv"
   args = ["--directory", "<absolute path to the unzipped connector folder>", "run", "inbox-zero-connector", "serve"]
   startup_timeout_sec = 60

   [mcp_servers.inbox-zero.env]
   INBOX_ZERO_PROVIDER = "microsoft"
   INBOX_ZERO_INSTALL_ID = "<the install id printed by setup>"
   ```

   On Windows the directory is written `C:\\Users\\<you>\\inbox-zero-connector`
   — double backslashes inside a TOML string — or with forward slashes.
   `uv run inbox-zero-connector paths` prints the install id on its
   `install id:` line.

2. Fully quit and reopen the ChatGPT app — Cmd-Q on macOS, close it from the
   tray on Windows. It reads `config.toml` only at launch.

3. Verify from the CLI:

   ```powershell
   codex mcp list
   ```

   `inbox-zero` appears in the list and is enabled.

4. In a Codex thread — a local thread, not a cloud task — the first tool call
   shows an approval prompt. Approve it.

5. Send this prompt:

   > Use the inbox-zero MCP tools. Run survey_inbox and tell me the message
   > count and the folders available.

   Expected result: a message count, `truncated` false, and the taxonomy
   folders listed.

Headless `codex exec` cannot use the connector: Codex auto-denies MCP tool
calls when there is no interactive approval.

## 9. Prove it works — stranger checklist

Run each; all must succeed on your machine:

1. `uv run inbox-zero-connector --version` → prints a version.
2. `uv run inbox-zero-connector paths` → every printed path is on this
   machine; after step 3 the taxonomy and config files exist.
3. `uv run inbox-zero-connector show-scopes` → matches step 6.
4. `uv run inbox-zero-connector auth microsoft` (re-run) → returns
   without prompting (silent token from the credential store).
5. `uv run inbox-zero-connector watch --once` → exit code 0; mail in
   the inbox gets triaged per the taxonomy. Exit code 1 means the round hit
   an error -- most often the credential from step 4.
6. `uv run inbox-zero-connector digest` → prints today's audit digest;
   the ledger file named by `paths` exists and grows as actions land.
6a. `uv run inbox-zero-connector digest --as-draft` → the same digest, also
   left as a draft in the owner's OWN mailbox, subject `Inbox Zero digest
   <YYYY-MM-DD>`, addressed to nobody. It cannot send, and it refuses with
   one line naming the flag when drafts are off for this account (the
   triage-only first-install posture), which is the expected result here.
6b. `uv run inbox-zero-connector list-folders` → prints every folder this
   install can enumerate and whether the granted scope covers it. A good
   result is the taxonomy folders from step 3 all present and covered; a
   folder listed as not covered is a scope problem, not a filing bug.
6c. `uv run inbox-zero-connector status` → reads the health file the watcher
   writes at the end of every round. A good result here is **exit 0** and a
   healthy line. **Exit 1** means it needs attention (stale or failing);
   **exit 3** means no health file exists yet, which is the expected answer
   if you have only ever run `watch --once` and never left a loop running.
   If the owner runs the watcher on a longer interval than the default,
   pass it: `status --interval 300`. Staleness is judged at 2x that value
   (default 60.0 seconds), so an unpassed interval reports a healthy
   long-interval watcher as stale.
7. In Claude Desktop, the `inbox-zero` server appears and its tools
   respond. If you registered Codex instead, `codex mcp list` shows
   `inbox-zero` enabled and a Codex-tab thread runs `survey_inbox`.
8. No OTHER machine is watching this mailbox — ask the owner whether an
   earlier install is still running anywhere, and stop it if so (see 6b §
   One watcher per mailbox). Nothing checks the one-watcher rule for you:
   `status` is the health surface for THIS install only. It answers "is my
   watcher alive," never "is someone else's watcher on the same mailbox,"
   and two racing installs both report healthy. The question stays a
   question for the owner.

Steps 5 and 6 filing Microsoft mail on a real M365 mailbox is the
expected result: that path was verified end to end on a live tenant on
2026-08-22 (issue #98 receipt).

## 9b. Updating

Run `uv run inbox-zero-connector update-check` to ask whether a newer build
of your tier has been published. It downloads nothing and changes nothing;
if it cannot reach us it says so and your install is unaffected.

When it reports one, download the new zip from the link it prints and run
`install.sh` (macOS/Linux) or `install.ps1` (Windows) from the unzipped
folder. Nothing ever replaces itself: your config, keychain entries and
ledger are kept.

## 10. Send diagnostics (beta)

When something did not behave the way you expected, run:

```powershell
uv run inbox-zero-connector diagnose        # or: diagnose --out DIR
```

It writes one zip: the per-message decision trace, the audit ledger,
watch state, your installed config and taxonomy, the scope manifest,
the last seven days of digests, version info, and the tail of each log.

It never contains `secrets.json`, any token (those stay in the OS
keychain), or any mail content -- no subjects, no bodies, no sender
addresses; sender domains only.

Send the zip back to rTech with a sentence about what you expected to
happen.
