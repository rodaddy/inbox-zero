# Every write this connector can perform

Status: WRITTEN 2026-09-02, enumerated from a source read of `src/` at
`feat/131-lane` (origin/main `0877fb4`). Companion to
`../what-leaves-the-machine.md`, which covers EGRESS -- which bytes go where.
This one covers MUTATION -- what this software can change, in the mailbox, on
the disk, in the keychain, and on the wire.

The claim under test is ephemerality: that the connector holds no mail, sends
nothing, deletes nothing, and leaves behind only receipts the owner can read
and reverse. The way to prove that is not to assert it, it is to enumerate
every write and let the list be short enough to check. Every row below cites
`path:line` from the tree. Anything not read out of the tree is marked
ASSUMPTION.

## 1. Mailbox writes through the provider surface

The provider interface is the ONLY way this package touches a mailbox. It
declares eleven operations (`src/inbox_zero_connector/providers/base.py:362`
through `:562`), of which **six are reads** -- `authenticate`, `list_inbox`,
`get_thread`, `list_containers`, `poll_changes`, `read_sent_sample` -- and
**five are writes**. Those five are the whole mailbox mutation surface:

| Op | Contract | Gmail impl | Graph impl | What it writes | Triggered by |
| --- | --- | --- | --- | --- | --- |
| `file_message` | `src/inbox_zero_connector/providers/base.py:487` | `src/inbox_zero_connector/providers/gmail/provider.py:883` | `src/inbox_zero_connector/providers/outlook/provider.py:1134` | Adds the taxonomy label and removes `INBOX` (Gmail, `provider.py:898`); `POST .../move` into the folder (Graph, `provider.py:1155`) | Watcher `src/inbox_zero_connector/services/watch/round.py:1833`; MCP verb `src/inbox_zero_connector/apps/server/main.py:678`; undo `src/inbox_zero_connector/services/audit/undo.py:255`; intent wrapper `src/inbox_zero_connector/services/audit/intents.py:154` |
| `tag_message` | `src/inbox_zero_connector/providers/base.py:510` | `src/inbox_zero_connector/providers/gmail/provider.py:901` | `src/inbox_zero_connector/providers/outlook/provider.py:1234` | ADDS one label in place. Never removes, never moves | **No caller in `src/` outside the two adapters** (see note below) |
| `set_important` | `src/inbox_zero_connector/providers/base.py:506` | `src/inbox_zero_connector/providers/gmail/provider.py:937` | `src/inbox_zero_connector/providers/outlook/provider.py:1286` | Raises/lowers the provider's importance flag. Reversible | MCP verb `src/inbox_zero_connector/apps/server/main.py:804`. Explicitly FORBIDDEN to the watcher: `src/inbox_zero_connector/services/watch/round.py:237` |
| `create_draft` | `src/inbox_zero_connector/providers/base.py:549` | `src/inbox_zero_connector/providers/gmail/provider.py:985` | `src/inbox_zero_connector/providers/outlook/provider.py:1305` | A reply draft on an EXISTING thread. Recipients derived from the thread, never passed in | Watcher `src/inbox_zero_connector/services/watch/round.py:2073`; MCP verb `src/inbox_zero_connector/apps/server/main.py:882` |
| `create_self_draft` | `src/inbox_zero_connector/providers/base.py:562` | `src/inbox_zero_connector/providers/gmail/provider.py:1041` | `src/inbox_zero_connector/providers/outlook/provider.py:1349` (helper `src/inbox_zero_connector/providers/outlook/self_draft.py:51`) | A draft with empty To/Cc/Bcc in the owner's own mailbox | `digest` CLI verb only: `src/inbox_zero_connector/apps/cli/digest.py:118` |

Two writes are structural side effects of the above rather than operations of
their own, and both are named here so the list is complete:

| Site | What is written | Trigger | Violation shape |
| --- | --- | --- | --- |
| `src/inbox_zero_connector/providers/gmail/provider.py:833` | `users.labels().create` -- creates a Gmail label for a taxonomy folder that does not exist yet | Reached from `_label_id` inside `file_message`/`tag_message` on a fresh install or a newly added folder | A label created from anything but the installed taxonomy; a `labels().delete` or `labels().patch` appearing at all |
| `src/inbox_zero_connector/providers/gmail/provider.py:879` | `users.messages().modify` -- the single Gmail mutation primitive all three Gmail writes funnel through | `file_message`, `tag_message`, `set_important` | A `removeLabelIds` containing anything other than `INBOX`; a call to `threads().modify`, `messages().trash`, `messages().delete`, or `messages().send` |

**What would make any row a violation.** For the five interface operations the
answer is one shape: a parameter or a call that could ADDRESS or SEND mail. A
recipient parameter appearing on `create_draft` or `create_self_draft` is the
canonical one -- the contract argues this at `providers/base.py:549` and `:562`,
and the absence of a `to`/`cc`/`bcc` argument is the security property, not a
convenience. Beyond that: any `send_message`, `send_draft`, `reply`,
`reply_all`, `forward`, `delete`, `trash`, or `purge` method appearing on
`MailProvider` or on either adapter; a `file_message` that reached a folder not
in the closed taxonomy; a `tag_message` that removed an existing label or
changed a container; a twelfth operation added to `MailProvider` without the
security-surface test being updated to match. `tests/test_security_surface.py`
pins the count, so adding one and leaving that test untouched fails.

**`tag_message` has no caller.** `rg` over `src/` finds it defined in
`providers/base.py:510` and implemented at `gmail/provider.py:901` and
`outlook/provider.py:1234`, and invoked from nowhere else in the package. It is
a declared capability with no live call path. That is a security-favourable
fact (the surface is smaller in practice than on paper) and it is recorded here
rather than smoothed over. Whether it SHOULD have one is a product question
this exhibit does not answer.

## 2. Disk writes

Every durable file the connector creates. All paths are under the connector's
own config/data directory (`src/inbox_zero_connector/config_paths.py`) unless
stated.

| Writer | What is written | Trigger | Violation shape |
| --- | --- | --- | --- |
| `src/inbox_zero_connector/utils/atomic_write.py:46` | `write_text_atomic` -- the shared primitive: mkdir parent, temp sibling, fsync, rename. Not a file of its own | Called by the writers below | Bypassing it for a durable file, leaving a torn write possible |
| `src/inbox_zero_connector/services/audit/ledger.py:582` (path + mkdir), `:605` `_write_row` (append + fsync) | The audit ledger: one append-only JSON row per mutation -- provider, action, message id, folder, run id, account, status | Every mailbox write, via `ledger.append` | A row deleted or rewritten in place; a mutation performed with no row (the code raises rather than swallow, `ledger.py:624`); a draft BODY appearing in a row |
| `src/inbox_zero_connector/services/audit/intents.py:59` | Write-ahead intent rows (JSON lines) recorded BEFORE a filing, so a crash mid-move is recoverable | `intents.py:137` begin/settle around `file_message` | Intents retained after settle; an intent naming a folder outside the taxonomy |
| `src/inbox_zero_connector/services/audit/intents.py:119`, `:122` | Rewrites the intent file to drop settled rows (truncate to empty at `:119`) | Settle path | Truncating rows that had not settled |
| `src/inbox_zero_connector/services/audit/trace.py:188` | Diagnostic trace rows: provider, message id, thread id, from-DOMAIN (not address), folder before/after, result | Each triage decision | A full sender address or a subject line appearing in a trace row |
| `src/inbox_zero_connector/services/watch/state.py:1639`-`:1640` | Watcher resume state -- the opaque provider cursor and per-run carryover | End of each watcher pass | Message CONTENT persisted into state rather than ids/cursors |
| `src/inbox_zero_connector/services/watch/health.py:92` | Health file: last run, counts, last error | Each watcher pass | -- |
| `src/inbox_zero_connector/services/triage/learned.py:311`, `:316` | Learned rules -- the owner's own corrections, replayed locally so those messages never reach a classifier | `record_correction` | A learned rule storing message bodies |
| `src/inbox_zero_connector/services/triage/taxonomy.py:615` | The installed folder taxonomy (names + descriptions) | Install interview / taxonomy edit | -- |
| `src/inbox_zero_connector/services/voice/profile.py:398`, `:406` | The voice profile derived from the sent-mail sample | `voice` CLI verb | Raw sent-message bodies persisted rather than the derived profile |
| `src/inbox_zero_connector/services/auth/grants.py:582` | The recorded grant/scope set for an authenticated provider | `auth` verb | A TOKEN VALUE written here instead of the keychain -- see section 3 |
| `src/inbox_zero_connector/config_writer.py:121`/`:139`, `:203`/`:204`, `:357`/`:361` | `connector-config.json` and the secrets-file skeleton. This module is the single seam that can modify config (`config_writer.py:7`) | `init-config`, `setup`, installer | A real secret VALUE written into the config file; config written from anywhere but this module |
| `src/inbox_zero_connector/utils/logging_config.py:767` (mkdir), `:769` text sink, `:771` JSON sink | Rotating log files `inbox-zero-<env>.log` / `.jsonl`, gz-compressed on rotation | Any run, when `file_sink` is on | A message body, subject, or token appearing in a log line |
| `src/inbox_zero_connector/apps/cli/diagnose.py:313` (mkdir), `:317` zip | A support bundle zip -- installed files, scopes, 7-day digest, versions -- written to CWD by default, redacted through `_secret_values()` (`:316`) | `diagnose` CLI verb, explicit operator action | A secret surviving redaction into the bundle |
| `src/inbox_zero_connector/services/reset/fixture.py:143` | Test-fixture snapshot JSON | Reset/fixture tooling, not a production path | Running against a real mailbox outside a reset |

**No cache of mail exists.** There is no message store, no body cache, and no
attachment directory in this list, because none is written anywhere in `src/`.
The disk holds configuration, receipts, cursors, and derived rules -- not mail.
That is the ephemerality claim in its concrete form.

## 3. Keychain writes

Exactly one module touches the OS keychain, and it does two things.

| Site | What is written | Trigger | Violation shape |
| --- | --- | --- | --- |
| `src/inbox_zero_connector/services/auth/keychain.py:155` | `keyring.set_password(SERVICE_NAME, _key(provider), json.dumps(payload))` -- the OAuth token payload, under a per-provider key (`_key`, `:85`) | `store_token` (`:138`), from the `auth` flow | A token written to a file on disk instead; a payload logged or echoed; a key not namespaced per provider |
| `src/inbox_zero_connector/services/auth/keychain.py:232` | `keyring.delete_password(...)` -- removes the stored token | `delete_token` (`:213`), from `auth` revoke / `uninstall` | Deleting a credential the connector did not create |

`google_client_config` (`:245`) and `microsoft_client_config` (`:264`) READ
client identifiers only and write nothing.

## 4. Network writes

Every outbound request with a mutating verb. Reads (`GET`, and the Google SDK's
`list`/`get` calls) are out of scope for this section and are covered by
`../what-leaves-the-machine.md`.

| Site | Verb | Host | What is written | Violation shape |
| --- | --- | --- | --- | --- |
| `src/inbox_zero_connector/providers/outlook/provider.py:735` `_post`, executed at `:753` | `POST` | `https://graph.microsoft.com/v1.0` (`provider.py:103`) | The generic Graph POST seam. Reached by: folder create (`:1126`), `/move` (`:1155`), `createReply` draft (`:1334`) | Any POST to a `/sendMail`, `/send`, `/forward`, or `/reply` path. `createReply` is pinned as the only allowed draft action by `tests/test_security_surface.py` |
| `src/inbox_zero_connector/providers/outlook/provider.py:756` `_patch`, executed at `:790` | `PATCH` | `https://graph.microsoft.com/v1.0` | Category union write-back for `tag_message` (`:1276`, If-Match guarded), importance flag for `set_important` (`:1302`) | A PATCH carrying `toRecipients`, `ccRecipients`, or `bccRecipients`; a PATCH that clears categories the owner set |
| `src/inbox_zero_connector/providers/gmail/provider.py:879` | Google SDK `messages().modify` (HTTPS POST under the hood -- ASSUMPTION: the verb is the SDK's, not written literally in this tree) | `www.googleapis.com` (via `googleapiclient`) | Label add/remove on one message | `removeLabelIds` beyond `INBOX`; any `send`, `trash`, or `delete` method reached on the SDK client |
| `src/inbox_zero_connector/providers/gmail/provider.py:833` | Google SDK `labels().create` (ASSUMPTION: same -- the verb is the SDK's) | `www.googleapis.com` | A new Gmail label named for a taxonomy folder | A label name not from the installed taxonomy |
| `src/inbox_zero_connector/providers/gmail/provider.py:1041` `create_self_draft`, `:985` `create_draft` | Google SDK `drafts().create` | `www.googleapis.com` | A draft, never sent | `drafts().send` appearing anywhere |
| `src/inbox_zero_connector/services/reset/fixture.py:341`-`:345` | Google SDK `messages().modify` | `www.googleapis.com` | Restores a fixture mailbox to its snapshot placement, additively | Running outside a reset/fixture context |
| `src/inbox_zero_connector/services/auth/keychain.py:258` | Token exchange POST to `https://oauth2.googleapis.com/token` (ASSUMPTION: the POST is performed inside `google-auth`; this tree supplies the URI only) | `oauth2.googleapis.com` | OAuth code/refresh exchange | -- |
| `src/inbox_zero_connector/services/auth/keychain.py:275` | MSAL token exchange against `https://login.microsoftonline.com/<tenant>` (ASSUMPTION: same -- the POST is MSAL's) | `login.microsoftonline.com` | OAuth token exchange | -- |
| `src/inbox_zero_connector/classifiers/openai_compatible.py:229` | POST chat-completions | The client's own `classifier.base_url`, null by default | Sender, subject, 500-char snippet, taxonomy. Fully enumerated in `../what-leaves-the-machine.md` | Any message BODY or attachment in the payload |

`tests/test_containment.py` asserts that no source file contains an `http(s)`
literal outside an allowed set, so a new host cannot be introduced quietly.

## What this exhibit proves, and what it does not

It proves that the mutation surface is small, enumerable, and structurally
incapable of the things the product promises it will not do: five mailbox write
operations exist, none of them takes a recipient, no send/delete/forward method
is declared on the interface or on either adapter, every mailbox write is
receipted to an append-only ledger that refuses to be skipped, and the only
credential store is the OS keychain. It also proves the ephemerality claim in
its concrete form -- the disk write list contains configuration, receipts,
cursors, logs, and derived rules, and contains no message store, body cache, or
attachment directory, because no code in `src/` writes one.

It does NOT prove runtime behaviour. This is a source enumeration, valid for the
tree at `origin/main 0877fb4` and no other; it is WRITTEN, not RUNNING. It does
not cover what a dependency writes on its own account -- `google-api-python-client`,
`msal`, `keyring`, and `loguru` each write caches, token stores, and rotated
files of their own, and the three ASSUMPTION rows in section 4 exist precisely
because the HTTP verb is the library's and not visible in this tree. It says
nothing about what a client's classifier endpoint retains after receiving a
request, and nothing about a mailbox write performed by anything other than this
connector. It is an inventory of capability, not an audit of a running install.
