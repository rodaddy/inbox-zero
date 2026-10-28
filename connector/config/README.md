# `config/` — committed, diffable settings

Everything here is **tracked in git**. A posture, a mailbox scope, a model
name, and a log level are not secrets, and hiding them behind a gitignored
directory is how shared defaults stop being shared. This folder is how the
product is READ and how it is DISTRIBUTED: an installer copies these files to
the per-user config dir; a reader learns every setting the connector has
without installing anything.

| File | Purpose |
|---|---|
| `connector-config.json` | Defaults every install inherits — the file the installer copies to the OS user-config dir (`platformdirs`: on macOS `~/Library/Application Support/inbox-zero-connector/`) |
| `connector-config.test.json` | Test-environment overrides, layered on top |

## The split with `secrets/` is per-key

```
config/connector-config.json   credentials.google_client_id, credentials.microsoft_client_id, credentials.microsoft_tenant_id
secrets/config.json            credentials.google_client_secret, credentials.classifier_api_key
```

Same `credentials` group, two files. A client ID and a tenant ID identify an
app registration and are safe on a whiteboard; the client secret and any API
key are not. One sensitive field does not make its siblings sensitive.

`microsoft_client_id` ships FILLED IN, with rTech's own multi-tenant
public-client registration, so a Microsoft install signs in without
registering anything. That is the same whiteboard test passing, not an
exception to it: a public client has no secret, and the user's token is minted
to their own keychain. `microsoft_tenant_id` is deliberately absent from the
file so the code default `common` applies, which admits work, school, and
personal Microsoft accounts. A client who wants the registration in their own
tenant overrides both keys. Google stays `REPLACE-ME`: its desktop flow needs a
client SECRET, so bring-your-own is the only correct arrangement there.

OAuth USER tokens are in neither file, ever: they live in the OS keychain only
(`auth/keychain.py`), which is the Guardian no-token-on-disk guarantee.

Test for placement: **would this be safe on a whiteboard?** If yes it belongs
here. Only credentials go in `secrets/`.

## Load order — current state, stated exactly

In code, proven by `tests/test_config_layering.py`. Highest precedence first:

1. init kwargs — tests only
2. environment variables (`INBOX_ZERO_*`, nested with `__`)
3. `secrets/config.json` — project-directory credentials, gitignored
4. `<config dir>/secrets.json` — the installed per-user credential file that
   `setup` creates and `paths` prints
5. `connector-config.json` in the platformdirs user-config dir — the installed
   per-user file
6. `config/connector-config.{env}.json` — committed per-environment overrides
7. `config/connector-config.json` — committed defaults, this file
8. code defaults (`src/inbox_zero_connector/config.py`)

Layers 3-7 are ONE `JsonConfigSettingsSource` over a list with
`deep_merge=True`, declared lowest-first in `settings_customise_sources`
(`config.py`, `_layered_files`) — that list IS the order, so it cannot drift
from this table.

**Two credential files, and they are not the same file.** Layer 3 is
`secrets/config.json` inside the project directory; layer 4 is `secrets.json`
in the per-user config directory. `setup` creates layer 4, and
`uv run inbox-zero-connector paths` prints its exact location — that is the
one to fill in on a normal install. Layer 3 exists for a checkout run in place.
Filling in the wrong one produces a silent no-credential run, so use `paths`
rather than guessing.

**Layers 3 and 4 are CREDENTIALS ONLY, and the code refuses anything else.**
A credential layer stating any top-level key other than `credentials` — a
`posture`, a `drafts_enabled`, a `classifier` — fails the load with a message
naming the file and the offending keys. That refusal lives in
`config_paths.checked_credential_files`, which is the one function every load
path goes through to reach those files, so no construction route reaches them
unscreened. It refuses rather than silently dropping the key: somebody put it
there on purpose, and a value that quietly does nothing strands them as badly
as one that quietly does something (#156, landed).

The two halves are therefore nameable apart, and that split is what the posture
cross-check depends on. The AUDITED layers (5, 6, 7 in this table —
`config_paths.audited_config_files`) are the ones that state the deployment's
decisions and the only ones the posture check reads; layers 3 and 4 carry
values nobody audits and can no longer state a decision at all.

`deep_merge` is what lets
`secrets/` overlay the `credentials` group instead of replacing it, so a client
secret lands beside the committed client ID rather than blanking it.

A missing file is skipped, not an error, so **a fresh clone runs from the
committed files alone** — no install step and no exported variables. The
per-environment layer is selected by `INBOX_ZERO_ENV`.

One field is exempt from ordinary precedence: `drafts_enabled`. It is the
no-send SECURITY POSTURE rather than a preference, so an environment value that
CONTRADICTS what these files state fails the startup naming both sources
(`_env_may_not_flip_the_posture`). It reads the AUDITED layers only
(`_file_posture` → `audited_config_files`), so the file a human is pointed at is
the file the guard compares against — a `secrets.json` cannot satisfy it or
answer for it.

## `logging.level`

Accepted values are the loguru level names. `LoggingSettings.level` in
`src/inbox_zero_connector/config.py` types the field as
`Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]`, default `"INFO"`,
so anything else fails at construction with a message naming the field.

At `INFO` you get one line per branch: the five-point wrapper's exit/result
line, plus the warning points (fallback, guard, retry) and errors. `DEBUG`
adds the entry point, which the wrapper emits per I/O call -- a DEBUG line per
outbound provider call and per MCP tool invocation, not per statement -- so the
full input/output trace of every provider and classifier call is visible.

Message content is structurally unrenderable at any level. Content travels as a
bound field, never as message prose: the call API only accepts identifiers, an
AST guard fails the build on a content-named value built into a message string,
and a redacting patcher sweeps every record as a backstop. Raising the level
does not expose bodies, subjects, addresses, or tokens.

The tracked default stays `INFO`. rTech beta installs get `DEBUG` via a
pre-configured zip (the `scripts/packaging/build_custom.py` profile overlay),
never by editing this tracked file.

## `logging.console_level`

Same accepted values, plus unset. It levels the stderr sink ALONE; the file and
JSON sinks keep following `logging.level`. Unset -- the default -- means the
console follows `level` too, so a config naming only `level` behaves exactly as
it did before this key existed.

The case it exists for is a support install: `level` at `DEBUG` for the full
trace on disk, `console_level` at `WARNING` so the person running the CLI sees a
quiet terminal instead of a DEBUG line per provider call. It cannot move the
console off stderr -- stdout carries the MCP JSON-RPC frames and no setting
reaches it.

## `classifier`

`classifier.base_url` defaults to null: no model is called and anything the
deterministic rules do not settle is filed to Review. Set it to the client's
own OpenAI-compatible chat-completions endpoint, with `classifier.model` and
the key in `secrets/config.json` as `credentials.classifier_api_key`.
Per message the request carries sender, subject, and the first 500
characters of the preview snippet; never the body
(`docs/what-leaves-the-machine.md`). rTech-hosted classification is never a
default; beta zips that pre-fill it are built by
`scripts/packaging/build_custom.py`, not by editing this file.
