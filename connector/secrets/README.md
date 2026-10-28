# `secrets/` — credentials only

Copy `config.example.json` to `config.json` and fill in real values. That copy
is gitignored. This directory is an **allowlist**: everything is ignored except
`.gitkeep`, `*.example.json`, and this README, so a file of real values cannot
be committed by forgetting to update a blocklist.

## What belongs here

Credentials, and nothing else. The test: **would this be safe on a
whiteboard?** If yes, it goes in `config/`.

| `secrets/` | `config/` |
|---|---|
| `credentials.google_client_secret` | `credentials.google_client_id` |
| `credentials.classifier_api_key` | `credentials.microsoft_client_id`, `credentials.microsoft_tenant_id` |

`credentials` appears on **both** sides. The split is per-key, not per-group —
one sensitive field does not make its siblings sensitive. That is the most
common way this gets done wrong.

## What NEVER belongs here, even filled in

OAuth USER tokens. They are written to the OS keychain only
(`src/inbox_zero_connector/services/auth/keychain.py`) and never to any file.
A `secrets/` folder holds an app-registration credential, not the mailbox
credential.

In practice that means the Google client secret. There is no Microsoft entry
because there is no Microsoft secret: the kit ships rTech's multi-tenant
PUBLIC client, which by definition has none, and a client using their own
Entra registration registers it as a public client too. `microsoft_client_id`
and `microsoft_tenant_id` are therefore identifiers in `config/`, never
values here.

## Load order

Environment variables (`INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_SECRET`, etc.)
outrank every file. Among the files this one wins: `config.json` here is the
LAST JSON layer applied, above `<config dir>/secrets.json`, above the installed
per-user file, and above everything committed in `config/`. It is merged
key-by-key (`deep_merge`), so supplying only `credentials.google_client_secret`
here leaves the client ID from `config/` intact rather than replacing the whole
`credentials` group.

**This is the project-directory file, and it is not the one a normal install
uses.** `setup` creates a separate `secrets.json` in the per-user config
directory; `uv run inbox-zero-connector paths` prints its location. Fill that
one in unless you are running from a checkout in place. Two credential files
that differ by name and directory are easy to confuse, and filling in the wrong
one produces a silent no-credential run.

Full table, in code and proven by `tests/test_config_layering.py`:
`config/README.md` § Load order.
