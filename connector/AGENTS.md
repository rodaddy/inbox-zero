# Agent Installer Contract — Inbox Zero Connector

You are an AI agent that has been handed this folder (or a zip of it) to set
up the Inbox Zero connector for the person you are talking to. Everything you
need is in this directory. Do not search the internet for install steps; the
repo says how to run itself.

(If instead you are a developer agent working on this connector's SOURCE
inside the rtech-consulting repo, the repo's own policy files govern you, not
this one — this contract is for the shipped kit.)

## Your job

1. Read `INSTALL-RUNBOOK.md` in this directory, in full, before acting. It is
   the complete, ordered install path — Windows PowerShell first, macOS/Linux
   beside. `README.md` holds the background and security rationale; cite it
   when the user asks why.
2. **If you can execute commands** (Claude Code, Codex CLI, or an MCP shell):
   the machine setup is ONE deterministic script — run it, do not hand-type
   the sequence it owns:
   - Windows: `.\install.ps1 -OwnerDomain <their-mail-domain>`
   - macOS/Linux: `./install.sh --owner-domain <their-mail-domain>`
   It is idempotent (safe to re-run) and ends with a verify pass. Then run
   runbook steps 7 and 9 yourself, showing the user each command and its
   output. For step 8, `uv run inbox-zero-connector desktop-config
   google|microsoft` prints the exact JSON block; you may merge it into the
   user's `claude_desktop_config.json` — show the diff before writing.
3. **Steps only the human can do** — do them WITH the user, never claim to do
   them for the user:
   - Step 4, Microsoft admin consent: the kit already carries rTech's app
     registration, so there is nothing to register. On a BUSINESS tenant a
     Global Administrator grants consent once for `Mail.ReadWrite` (the
     runbook gives the admin-consent URL); Entra will show rTech as an
     unverified publisher, which is expected. On a personal account, or a
     tenant that allows user consent, the first sign-in prompt in step 5 is
     the whole of it. Only if the client insists on their OWN registration
     do you walk the portal clicks, which the runbook keeps as an advanced
     option.
   - Step 5, sign-in: the command opens the mailbox owner's own browser for
     consent; wait for them. Only if it falls back to a device code, give
     them the code and URL it prints (a security-defaults tenant will refuse
     that path — the fix is the `http://localhost` redirect in Step 4).
4. **If you cannot execute commands** (chat-only desktop app): have the user
   run the one install script above first, then walk the runbook's remaining
   steps one at a time, one command per message, asking for the output before
   moving on.

## Hard rules — these are the product's security posture, not suggestions

- NEVER enable drafts on a first install (`--enable-google-drafts` /
  `--enable-microsoft-drafts` stay off). Triage-only is the correct first
  posture; the runbook says when and how to revisit.
- NEVER ask the user to paste secrets, tokens, or client secrets into the
  chat. Non-secret IDENTIFIERS (`google_client_id`, `microsoft_client_id`,
  `microsoft_tenant_id`) may be entered at the `setup` wizard's prompts, via
  `init-config` flags, or as environment variables — env wins on conflict.
  SECRET VALUES (`google_client_secret`, `classifier_api_key`) are typed by
  the human into the secrets file the wizard points at (the path
  `uv run inbox-zero-connector paths` reports), or into environment
  variables — never into chat. Tokens live in the OS keychain. If a value
  would appear in your context, stop and route around it.
- rTech SHIPS the Microsoft app registration, and that is not a hole in the
  security posture — do not "fix" it by telling the user to register their
  own. It is a MULTI-TENANT PUBLIC CLIENT: it carries no client secret, the
  consent screen is the user's own, and the token MSAL mints goes straight
  into this machine's OS keychain. rTech never receives, stores, or can
  request that token, which is the claim the README makes and it still
  holds (`src/inbox_zero_connector/config.py`, `CredentialSettings`). What a
  business tenant owes is one admin consent for `Mail.ReadWrite`, not a
  registration. A client MAY still use their own registration by overriding
  `microsoft_client_id` (and `microsoft_tenant_id` if single-tenant) — offer
  that when they ask for it, never as the default path.
- ONE watcher per mailbox, across every machine — never set up a second one
  "so it keeps working when the laptop is closed." Two `watch` loops on one
  mailbox race the same messages: duplicate labels, moves that fight each
  other, and two ledgers that each look like the audit trail. Nothing detects
  it. Before you finish an install, ASK whether an earlier install is still
  running somewhere; an old machine nobody uninstalled is the usual culprit.
  If there is one, have the owner remove its scheduled job and run
  `uv run inbox-zero-connector logout google|microsoft` there — the provider
  positional is REQUIRED, and a bare `logout` errors. Add `--yes` when you are
  running it non-interactively; without a terminal it waits on a prompt.
  (Two DIFFERENT mailboxes on one machine is fine — that is runbook 6b.)
- The connector cannot send mail. Do not "helpfully" script around that.
- Prove the install with the runbook's step-9 checklist and show the outputs.
  An install without those receipts is not done.

## Verbs beyond the install

Verbs beyond the install path the owner may ask you about, each documented in
`INSTALL-RUNBOOK.md`: `status` (is the watcher healthy — exits 0/1/3, takes
`--interval`), `update-check` (is a newer build published), `undo` (preview a
run's filings; `--apply` reverses them), `rules list|disable` (read or turn off
a learned rule), `voice build|show|approve` (the AI tier's voice profile;
`approve` is what makes one usable), `list-folders` (what this install can see
and whether scope covers it), and `digest --as-draft` (the day's digest left as
a draft in the owner's own mailbox, never sent). Run `--help` on any of them
before describing it; this contract names them, the runbook explains them, and
the CLI is the authority on flags.

## If something fails

Report the exact command and its exact output, then check
`uv run inbox-zero-connector paths` and `show-scopes` before retrying.
Unresolved failures go to the vendor: rTech Digital Consulting
(https://rtech-consulting.com).
