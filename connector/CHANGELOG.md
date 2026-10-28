# Changelog — rTech Mailroom Connector

All notable changes to this connector. Versions follow the repo's patch-default
bump policy.

## 0.2.15 - 2026-10-21

### Fixed

- The packaging ship list (`scripts/packaging/tiers.json`) no longer names
  `CLAUDE.md`, which #713 folded into `AGENTS.md`; the build had failed on
  the missing entry (#1039).

## 0.2.14 - 2026-09-01

### Added

- Ledger schema 4: every receipt carries a `run_id` and the `source_container`
  the message left. The watch round mints one run id per `poll_once` and stamps
  both on each row, which is what makes a filing reversible from the receipt
  that proves it happened (#66).
- `undo --last` / `undo --run <id>` select one run, and `undo --apply` moves the
  mail back through the provider, writing an `undo` receipt per reversal. Dry
  run is still the default, so the destructive reading must be asked for by
  name.

### Changed

- `undo` plans from the LEDGER rather than the decision trace. It previously had
  to read the trace — a file its own module documents as a diagnostic — because
  the ledger recorded a filing's destination and no source, which is why no
  `--apply` could ship. A row written before schema 4 carries no source and is
  refused and counted, never guessed at.
- The watch round's audit-writing boundary moved to
  `services/watch/receipts.py`: the ledger row, the trace row, and the
  write-ahead intent. `round.py` was at the 500-code-line ceiling, and the
  honest reading was that two subsystems shared one file.

### Compatibility

- Additive and no migration. A v1–v3 row loads unchanged with both new fields
  empty, and a running watcher keeps appending correctly across the change.

## 0.2.13 - 2026-08-24

### Changed

- Evaluation license `LICENSE.md` ships in every zip: proprietary, one
  organization, internal staff only, no distribution or customer deployment
  without a separate agreement; `pyproject.toml` names it.
- The enhanced tier ships with `drafts_enabled.microsoft` true; drafting
  activates once a classifier endpoint and key are configured in `setup`.
- Customer zips no longer carry internal hosts, other customers' names,
  personal addresses, or internal paths; `docs/google-external-client-setup.md`
  is internal-only and no longer ships. The documented local classifier
  host is `localhost:11434`.

## 0.2.12 - 2026-08-24

### Fixed

- `_covered` distinguishes a container excluded by scope from one the listing
  never enumerated, and the new `RoundSummary.skipped_unenumerated_container`
  carries the second case; the third watch-loop streak names the listing rather
  than the scope, and the reset fixture's `_covered` was fixed alongside (#198).

### Added

- A `list-folders` CLI verb prints every enumerable container with its scope
  coverage (#198).
- `docs/m365-tenant-setup.md`, the explicit business-tenant admin-consent
  runbook. `INSTALL-RUNBOOK.md`'s M365 status is corrected to the 2026-08-22
  #98 receipt, and its section 4 points at the new file (#214).

### Verified

- Client id `4be80172` is current in the paid rTech tenant; no config change
  (#140).

## 0.2.11 - 2026-08-24

Round-4 hardening from the round-3 confirm findings. The 0.2.10 zips were
built at 6287cfd; nothing below is in them.

### Changed (redo)

Three areas that survived four fix/confirm rounds by producing a new
in-wording variant each time were redone around a single rule rather than
patched a fifth time.

- The drafting loader no longer guesses whose exception it caught. Everything
  raised while the add-on is being imported is the add-on's,
  `KeyboardInterrupt` excepted; the frame-walk, anchor, and install-file
  helpers are deleted (#203).
- One parser decides whether a Gmail label row is usable, and both readers of
  a listing use it, so a row with an unusable `id` is skipped and counted
  instead of being stringified into a container id (#202).
- `MailboxScope.well_known_tag` / `refusal` / `entry_names` are the only
  resolution `covers()`, the `only_listed` entry guard, and the WARN reason
  consult, so a Gmail `Archive` label names `include_archive` rather than a
  generic setting; `RoundSummary` enforces its own count partition (#201).

### Fixed (pass 4)

- `UNCOMMITTED_WINDOW_CAP` is a ceiling, not a floor: `_window_target`
  clamps to `min(max(target, SEEN_LIMIT), cap)`, so a re-offered backlog on a
  stream that never commits settles at 2,000 ids instead of growing with the
  backlog (#194).
- An already-seen id no longer resets the scope-misconfiguration streak, and
  an `only_listed` entry that matches no folder in the listing is named
  once per process on the first pass, with the closest listed names (#191).
- A voice add-on whose class body calls `sys.exit()` during import is
  contained, reported once, and cached; the discriminator is whether any
  traceback frame lies inside the add-on package, not the frame name (#189).
- `label_id_or_refuse` refuses a non-mapping label first, so a malformed
  `labels.create` response cannot reach `messages.modify`; a non-mapping
  `labels.list` response is refused instead of coerced into a table (#186).

- Daemon-mode `watch` warns once after 3 consecutive rounds that polled zero
  streams, naming the likely cause (an `only_listed` scope that matches no
  folder) and the fix; `--once` semantics are unchanged (#191).
- The runbook states that `INBOX_ZERO_INSTALL_ID` must be present on every
  invocation, service units included, because `init-config` does not persist
  it (#193).
- The commit-gated replay window is capped at 4 x `SEEN_LIMIT` with one WARN
  per stream when a cursor never commits (#194).
- Watch state rejects negative counts, non-string keys, and blank cursor
  tokens on load and starts fresh instead of coercing them (#188).
- A corrupt-state reset logs that the poison-message attempt budget was reset
  with it; the re-arm is deliberate and documented (#185).
- A BROKEN installed voice add-on (circular import, syntax error) is reported
  loudly once per process instead of being routed to the quiet not-installed
  arm or re-warned on every message; the discriminator is whether the add-on
  is on disk, not the exception's name (#189).
- Both Microsoft account-pin refusals prescribe `logout microsoft` before
  `auth microsoft`, since auth alone repeated the error; a whitespace-only
  stored pin is treated as absent (#190).
- `find_label_id` raises `ProviderError` naming the label, never a bare
  `KeyError`, when Gmail returns a matching label row without an id (#186).
- Test hygiene: the four server-test fakes carry the current `poll_changes`
  signature with a conformance test (#184); a conftest fixture resets loguru
  sinks after every test so a closed captured stream cannot leak handler
  errors into later suites (#187).

Second pass, from the round-4 adversarial confirm:

- The zero-stream alarm fires on Gmail (the default provider, always one
  stream) via a second signal: rounds that saw mail and filed none of it
  because every message was out of scope. A listing failure no longer resets
  the streak. The WARN names the configured scope mode (#191).
- A broken add-on PARENT package (syntax error, module-scope raise, missing
  dependency, `sys.exit`) is loud once and cached; the on-disk check honours
  its never-raises contract (#189).
- One WARN per folder when a filed message's destination container does not
  resolve on a per-folder stream, since the replay mark then falls back to
  the source window (#194).
- Gmail's label CREATE branch takes the same id guard as lookup; blank ids
  are refused, non-dict rows are skipped, and an exact match wins over a
  malformed case variant (#186).
- The test sink-reset fixture resolves `sys.stderr` at write time so a
  monkeypatched buffer is never pinned as the process sink (#187).
- The MCP server's startup arm renders every operator refusal, taxonomy
  included, as one line with no traceback; the runbook says what an omitted
  `INBOX_ZERO_INSTALL_ID` actually looks like on a scheduled job (#193).
- An exhausted (poison) message stays exhausted after its id ages out of the
  replay window. Watch state keeps a persisted per-stream `exhausted` record
  (capped at 4 x `SEEN_LIMIT`, WARN at the cap) that the round consults before
  classifying; eviction never re-arms the attempt budget, a corrupt-state reset
  is the only re-arm (#185).

Third pass, from the round-4 re-confirm:

- The drafting loader contains any `BaseException` raised while the add-on
  (or its parent or grandparent package) is imported, re-raising only
  `KeyboardInterrupt` and a `SystemExit` that did not come from a module
  body; the on-disk check recurses up the package path (#189).
- The unresolved-destination WARN keys on the adapter's answer, not on a
  `None` fallback: it fires when no destination id was reported (including a
  decoy name match) or no post-move id was reported, sits above the early
  return, and latches per (source container, folder) (#194).
- `list_containers` skips a non-dict Gmail label row with one DEBUG line
  instead of leaking `AttributeError`; a label id that is not a non-empty
  string is refused, never stringified or cached (#186).
- The Gmail scope alarm holds its streak across quiet polls (a round that saw
  no mail is not evidence the scope is right) and resets only when a round
  triages something; it warns once per streak. The `all_folders` text names a
  real check instead of a folder list that does not exist (#191).
- An abandoned message is skipped on every stream of the provider, not only
  the stream that abandoned it; the exhausted record never evicts a verdict
  (the cap is a reporting threshold, WARN once per stream); loading a state
  file written before the record logs one INFO line that a pre-upgrade
  abandonment may be retried for one more bounded cycle; the skip is logged
  at DEBUG once per stream and message. Abandonment is permanent until a
  corrupt-state reset, and the docstrings say so (#185).

## 0.2.10 - 2026-08-24

Round-3 gauntlet fixes. Every item below was reproduced RED before its fix and
re-verified by an adversarial confirm lane at head; two first-pass fixes
(#173, #161) were refuted by that swarm and re-fixed in the same release.

### Fixed

- A message moved on Microsoft is remembered in the DESTINATION folder's own
  replay window, keyed by the container id the adapter reports it moved into.
  The first fix matched the destination by name and broke under any folder
  prefix (the `rTech/` one real installs ship) and under duplicate folder
  names; the adapter now says where it put the mail and the watcher stops
  guessing (#173).
- Each per-folder Outlook stream polls its OWN delta path instead of
  cold-starting against the inbox, and cursors minted by the broken build are
  dropped once through the normal resync so every stream starts clean (#143).
- Replay-window ids age out only after their round's cursor has committed.
  A big backlog followed by trickle traffic used to evict its own ids while a
  withheld cursor could still re-offer them, reprocessing 100+ messages every
  round; the window now holds its occupancy until the cursor moves (#161).
- A structurally corrupt state file starts fresh like every other corruption
  instead of killing the watcher with a validation error (#182).
- A fresh install mints and persists its own `install_id` on the first
  `setup` or `init-config` run, so the keychain refusal added in 0.2.9 no
  longer leaves a fresh unzip with no way to bootstrap (#171).
- A first-run `watch --once` on an installed but unauthenticated kit prints
  ONE refusal line (`Authentication error: ... Run auth first`) and exits 1
  instead of two provider tracebacks. `ConfigError`, `AuthError`, and
  `TaxonomyError` now share an `OperatorRefusalError` base; the CLI handler,
  the logging boundary, and the watch loop's fatal carve-out key on the base,
  so no future refusal regresses to a traceback (#195, #193).
- A voice add-on whose own dependency fails to import is reported loudly and
  filing continues; it used to kill the round after the message was already
  filed. Draft failures are contained after the receipt so an applied message
  is never rewritten as failed (#174).
- A transport error inside classification or filing is contained like any
  other per-message failure; the blanket OSError re-raise that aborted the
  stream receiptless is scoped to ledger writes only (#179).
- `watch --once` exits 1 when the configured scope selected no stream, so a
  misspelled folder list no longer reports success to a scheduler (#175).
- `--interval` is validated at the parser: 0, negatives, NaN and infinity are
  argument errors before anything polls (#181).
- The Microsoft account is pinned by the signed-in UPN; two accounts sharing
  the msal cache no longer silently retarget the install. An unpinned
  ambiguous cache refuses instead of guessing (#149).
- Running the CLI as a module exits with the same status as the console
  script; it used to exit 0 on every refusal (#192).

### Docs

- Client-facing docs say what the code does: Gmail's no-send posture is
  code-layer on the shipped consent flow, the runbook and examples no longer
  instruct a re-consent that changes nothing, the beta note describes each
  zip's real classifier posture, config load order matches `_layered_files`,
  and the install script's step-8 command is runnable (#152, #167).

## 0.2.9 - 2026-08-24

### Fixed

- A watch round now drains the provider's change feed to exhaustion instead of
  filing only the first page, and the resume cursor is stored from the FINAL
  page rather than the first. A mailbox that changed more in one round than a
  single page holds no longer had its remainder silently skipped and the
  cursor advanced past it, which is how mail went missing without any error
  (#142, #144).
- A message the provider did not enumerate a folder for is refused outright
  rather than read. Filing a message whose location is unknown is a guess, and
  a guess about where mail lives is not something the connector will make
  (#144).
- A classifier or transport failure no longer kills the watcher. The failed
  message is receipted, left UNSEEN, and retried on the next round, so one bad
  message costs a retry instead of orphaning itself and taking the rest of the
  round down with it (#157, #158).
- A filed message is remembered under BOTH its pre-move and post-move ids,
  which ends the Microsoft refile loop -- Graph reissues the id on a move, so
  the connector kept meeting the same mail as a stranger and filing it again.
  A page larger than the dedup window no longer evicts its own marks mid-pass
  (#161).
- The watcher stops promptly on `SIGTERM` and checkpoints after every folder
  stream, so a stop or a restart loses at most the stream in flight instead of
  the whole round (#162).
- `watch --once` exits non-zero when the pass could not file mail, so a
  scheduler can alarm on it. A failed pass that exited 0 was indistinguishable
  from a quiet inbox (#160).
- Learned rules resolve the uncertainty folder through the INSTALLED taxonomy
  instead of a hardcoded name, so an install that renamed its taxonomy has
  that rename enforced rather than ignored (#168).
- A move receipt Microsoft declines to name records an explicit unknown-id
  sentinel instead of the stale pre-move id. A receipt that names the wrong id
  is worse than one that admits it does not know (#169).
- The basic tier installs and runs. Watch-core loads the voice add-on lazily
  and simply never drafts when it is absent, instead of failing to import at
  all -- which also means the basic zip now passes the packaging judge
  instead of shipping only if someone skipped it (#146).
- An uninitialized install refuses to touch the OS keychain. With no
  install_id the credential key used to degrade to the bare account name,
  which could silently adopt a token from an earlier install on the same
  machine; it now exits with a one-line configuration error telling the user
  to run setup (#171).

## 0.2.8 - 2026-08-24

### Changed

- The Microsoft authority default is `common`, not `organizations`.
  `organizations` refuses a personal Microsoft account outright, while rTech's
  registration is audience `AzureADandPersonalMicrosoftAccount` -- the old
  default narrowed the app below what its own registration allows, for no
  security gain.
- rTech's multi-tenant PUBLIC client
  (`4be80172-cace-49cd-bbee-cef34841c8fb`) ships as the default
  `credentials.microsoft_client_id` in `config/connector-config.json`, so a
  Microsoft install no longer requires the buyer to create an Entra app
  registration. A public client carries no secret and the user's token is still
  minted straight into their own OS keychain, so the no-token-chain guarantee
  is unchanged. A client may still point at their own registration by
  overriding `microsoft_client_id` (and `microsoft_tenant_id` when
  single-tenant). Google remains bring-your-own, because its desktop flow
  requires a client SECRET.
- Kit docs follow the new path: `AGENTS.md` and `INSTALL-RUNBOOK.md` step 4
  describe admin consent for the rTech app -- with the
  `/{tenant}/adminconsent?client_id=...` URL and the unverified-publisher note
  -- and keep bring-your-own-registration as an advanced option.
  the beta note records that the beta customer now only owes admin
  consent (#65).

### Added

- `docs/what-leaves-the-machine.md`: the classification payload is exactly
  sender + subject + the first 500 characters of the preview snippet, never
  the body; the endpoint is the client's own (`classifier.base_url` default
  null means rules only); the Google unverified-app interstitial
  click-through. One-sentence versions in `INSTALL-RUNBOOK.md`,
  the beta note, and `config/README.md` (#130).
- the beta customer M365 profile is buildable: it rides the shipped rTech
  multi-tenant Microsoft client (no per-tenant registration, authority
  `common`), bundles NO classifier endpoint -- rules-only until the client
  configures their own -- and needs no vault secrets, so the zip carries an
  empty `secrets/config.json` (#65, #111).

## 0.2.7 - 2026-08-23

### Added

- `logging.console_level` levels the stderr sink independently of the file and
  JSON sinks. A support install can now run `level` at `DEBUG` for the full
  on-disk trace while the terminal stays at `WARNING`. Unset by default, in
  which case the console follows `logging.level` and existing installs are
  unchanged.

### Fixed

- Every CLI command printed two `SCOPES: enforcement` DEBUG lines above its own
  output. `scopes.NO_SEND_ENFORCEMENT` was a module-level constant computed by
  a function that logs, so the records were emitted at import, before
  `logging_config.setup` installed any sink -- reaching loguru's default stderr
  handler, which neither `logging.level` nor `logging.console_level` can level.
  The constant is now computed on first access; its name and value are
  unchanged.

## 0.2.6 - 2026-08-23

### Fixed

- The pydantic `UserWarning` about `LedgerEntry.schema` shadowing a `BaseModel`
  attribute is gone. It printed on every command; the field is now
  `schema_version` and the on-disk wire key stays `schema`.
- `watch --once` exits non-zero when the round recorded errors, so a cron job or
  wrapper script can tell a broken run from a clean one.

## 0.2.5 - 2026-08-23

### Fixed

- `install.sh` and every other `*.sh` are stored executable in the zips; the
  builders take modes from the git index rather than the checkout (#122 F1).
- A Gmail 404 on a message that vanished between listing and fetch is skipped
  as `skipped_missing` instead of crashing `watch` / `list_inbox` (#122 F3,
  follow-up #123).
- Installer next-steps name only the provider that was actually configured,
  not both (#122 F4).
- `show-scopes` closing prose matches the header enforcement layer, so the
  send-scope scan no longer trips on its own explanatory text (#122 F5).
- `ruff` selector is declared in `pyproject.toml` (#122 F7).

### Added

- Per-install isolation: `install_id` (from `config/connector-config.json` or
  `INBOX_ZERO_INSTALL_ID`) suffixes the config and data directories and the
  keychain account, so two installs on one machine never collide.
  `build_custom` defaults it to the profile name (#122 F2).
- `assert_zip --custom` mode for verifying custom-built zips (#122 F6).
- `docs/google-external-client-setup.md` (#117).

## 0.2.4 - 2026-08-23

Hold lifted for the beta customer (2026-08-23): the beta customer gmail
zip ships from this version on the customer's Workspace OAuth client. The beta customer M365
zip stays held on the MSP (#65). Account intake at install (#119)
remains pre-ship for general release.

### Fixed — Gmail flag labels are no longer containers (#118)

`providers/gmail/messages.py` reported every labelId as a container, and
`scope.covers_all` is any-exclusion-wins, so under the default `inbox_only`
scope a normal inbox message (INBOX + UNREAD, often IMPORTANT / CATEGORY_*)
was refused as out of scope — 3/3 skipped in the beta dress rehearsal.
Flags are not places: `GMAIL_FLAG_LABELS` + `_is_place` keep place labels
only in `containers`; `is_unread` / `is_important` still read the unfiltered
list. Gmail categories ride the new `Message.categories` field, which
`decide._is_gmail_promotional` now reads, so CATEGORY_PROMOTIONS ->
Newsletters stays live. RED-first in `tests/providers/gmail/
test_gmail_labels_and_watch.py`, `tests/services/watch/test_watcher.py`,
`tests/services/triage/test_classify.py`. Live: a test mailbox
`watch --once` -> `classified=1 applied=1` after the fix.

### Added — install transcripts, whole, in the diagnose bundle

`install.sh` / `install.ps1` transcribe every run to `logs/install-<UTC>.log`
(tee / `Start-Transcript` in `try`/`finally`); `logs/` is gitignored; CI
lists the transcript on both legs and fails when none was written.
`diagnose` ships `install-*.log` whole (scrubbed), skipping them in the
500-line tail pass so a coinciding log directory never duplicates a member.
`config/README.md` documents `logging.level`.

### Added — rTech-side custom-zip builder (never shipped)

`scripts/packaging/build_custom.py --profile profiles/<name>.json --out DIR`:
public tier members + the profile's overlay of `config/connector-config.json`
+ `secrets/config.json` resolved from Vaultwarden by entry NAME at build time.
Refuses a string secret leaf and any `--out` inside the repo; prints one
`BUILT ... sha256=...` line and never a value. `profiles/**` is `never_ship`.
Profiles: one gmail profile and one M365 profile (client ids TBD on #111 /
#65). the beta note rewritten for the pre-configured two-mailbox flow;
`INSTALL-RUNBOOK.md` gains the pre-configured-zip paragraph.

## 0.2.3 — 2026-08-22

Beta customer build, model-backed. Ships with the Bifrost classifier wiring
below; the 0.2.2 zip predates it and must not be sent.

### Fixed — the configured model classifier is now constructed for `watch` (#115)

`ClassifierSettings` and `classifier_api_key` had no consumer: `_cmd_watch`
built its round without a classifier, so every install ran `RuleOnlyClassifier`
and every unsettled message landed in Review. `classifiers/factory.from_settings`
is the single construction site (None when no `base_url` -> rule-only by
design; the backend's own `requires_key` check raises). Bifrost accepts the
SDK's `Authorization: Bearer` header (probed live, 200 on Bearer and
`x-bf-vk`), so no header override. Live smoke through Bifrost on
`tn01-llama/qwen3-4b-instruct`: 3/3 sensible folders, 0.5-1.0s.
RED-first: `tests/classifiers/test_factory.py`,
`tests/apps/cli/test_watch_classifier_wiring.py`.

### Added — the beta note ships in the zip

Moved into the connector and added to `tiers.json` ship; carries the Bifrost
base_url, model id, the exact `tier-basic.ps1` line, and the key-rotation
caveat. No key value anywhere in the kit.

### Added — `init-config --classifier-base-url / --classifier-model`; tier scripts on Windows

The vendor switch is now flag-settable (the wizard asked; scripts could not),
written whole via `write_config`; `requires_key` follows `base_url` and the
key itself is still never a flag. `init-config` moved to
`apps/cli/init_config.py` (main.py 504 -> 385 code lines).
`examples/tier-basic.ps1` / `tier-ai.ps1` mirror the `.sh` pair; both pairs
pass the classifier flags through. RED-first:
`tests/apps/cli/test_init_config_classifier.py`.

## 0.2.2 — 2026-08-22

Beta customer build (#111): the basic and AI tiers ship as ONE full zip plus
two install configs, with diagnostics beyond logs. WRITTEN and locally
tested; the M365 trial mailbox has not yet classified a message through
this build (live `watch --once` saw 2 out-of-scope messages, 0 classified).

### Added — per-message decision trace (`services/audit/trace.py`)

One content-free JSONL row per message the watch round CLASSIFIED, beside
the ledger (`decision-trace.jsonl`): provider, message id, thread id, sender
DOMAIN only, folder before/after, decision folder/confidence/reason
(capped at 200 chars), deterministic flag, result, error_code. Written
after the ledger receipt, with the receipt's message id, so trace and
ledger delta match by id. Out-of-scope and already-seen messages leave no
row. RED-first: `tests/services/audit/test_trace.py`.

### Added — `diagnose [--out DIR]` (`apps/cli/diagnose.py`)

Writes `inbox-zero-diagnostics-<UTC>.zip`: decision trace, ledger,
`watchstate-*.json`, `connector-config.json`, `taxonomy.json`,
`show-scopes` text, 7-day digest, versions, the last 500 lines of each log,
and a manifest. `secrets.json` is structurally never a member and every
string value in it is scrubbed from every member. RED-first:
`tests/apps/cli/test_diagnose.py` (planted secret never lands).
INSTALL-RUNBOOK.md section 10.

### Added — tier install configs

`examples/tier-basic.sh` (file_only, drafts off) and `examples/tier-ai.sh`
(file_read_draft + Microsoft drafts; `--google` adds Gmail drafts). Runbook
step 3 "Pick your tier". The tier is the config, not the code (#109 owns
the physically separate basic zip).

### Changed — `show-scopes` renderer moved to `apps/cli/show_scopes.py`

Byte-identical output; main.py dropped from 499 to 468 code lines so the
new verb fits under the 500 ceiling, and the diagnose bundle captures the
same renderer.

### Fixed — silent `auth microsoft` names the bound mailbox (#114)

`acquire_token_silent` returns no `id_token_claims`, so every re-run after the
first consent printed `Authenticated as unknown`. `reauth.account_name` now
falls back to the msal cache entry's `username` (the UPN msal indexes by);
claims still win when present. RED-first:
`tests/providers/outlook/test_outlook_silent_auth_name.py`.

### Changed — Microsoft sign-in is browser (PKCE) first, device code second (#112)

Entra security defaults — on by default in every new tenant — block the
device-code flow outright (AADSTS530035, seen live 2026-08-22 on the trial
tenant). `auth microsoft` now runs msal `acquire_token_interactive` on the
`http://localhost` loopback redirect first and falls through to the device
code only when the browser flow cannot complete. New module
`providers/outlook/consent.py` owns both flows (provider.py is at the size
ceiling); the T20/stdout exemption moves with it. App-registration docs now
require the mobile-and-desktop `http://localhost` redirect. RED-first:
`tests/providers/outlook/test_outlook_auth_flow.py`.

### Added — per-tier zip packaging: builder + judge (#107 slice 2)

`scripts/packaging/`: `tiers.json` (data manifest: ship allowlist,
never-ship refusals incl. internal process files per the 2026-08-21 ruling,
per-tier exclusions), `build_zips.py` (deterministic, byte-stable, ships
git-TRACKED files only), `assert_zip.py` (judge, exit 0/1/3, scanner-style
proof the artifact lacks what its tier withholds). CI builds and asserts
the FULL zip and uploads it as an artifact. The BASIC zip is blocked on a
real seam restructure — the drafting contract spans providers/base, config,
scopes, and the server (63 assertion violations across 30 files); see the
restructure issue. Full zip smoke: installs from the artifact and passes
its shipped suite (1571 passed, 36 xfailed). **WRITTEN and locally tested;
not shipped.**

### Added — config-file credentials and the `setup` wizard (#107 slice 1)

Credential IDENTIFIERS (`google_client_id`, `microsoft_client_id`,
`microsoft_tenant_id`) now load from the installed `connector-config.json`,
and secret VALUES from a per-user `secrets.json` beside it in the config dir
(`secrets_path()`, a new JSON layer between the installed file and the repo's
dev `secrets/config.json`; environment still wins on conflict). `paths`
prints the secrets file. The write surface moved to `config_writer.py`
(`config.py` was at its code-line ceiling; same seam precedent as
`graph_evidence.py`) and gained identifier/classifier persistence plus
`write_secrets_skeleton()`, which creates the file empty for the human to
fill — no code path writes a real credential. New interactive `setup` verb
(`apps/cli/setup.py`): TTY-only, refuses under MCP like `auth`, asks the
install questions with the scope consequence at the choice, never prompts
for a secret value, and calls the same writers `init-config` uses. Installers
and the runbook point at it. **WRITTEN and locally tested; not shipped.**

Review-round hardening on top of 0.2.0, plus two added tools. No
version bump: `VERSION` is still `0.2.0`, and nothing here has been released.
The tool surface is now **ten tools** — the read-only `survey_inbox` and the
mutating `record_correction` were added; the other eight are unchanged in
behaviour and wire schema. **WRITTEN and locally tested; not deployed, not
dogfooded, not shipped.**

### Added — record_correction, the correction surface

A tenth tool, and the only path in the connector that writes a learned filing
rule. The owner says a message belongs elsewhere; the message moves (the same
implementation `file_message` uses) and a rule is written so the correction is
never needed twice. Two ledger rows, one per mutation: the `file_message`
receipt for the mailbox change and one `record_correction` row for the rule.

Interactive capture ONLY. Nothing infers a correction from mailbox state, from
a move the connector itself made, or from the classifier being overruled — see
`corrections.py` for why inference is refused rather than merely unimplemented.
The response names the rule and the outcome, so the owner sees what was learned
at the moment it is learned. The attribution language throughout is "matches
your rule", never "filed by your rule": a rule is a deterministic test, not
something acting on the mailbox on its own.

Every change below narrows what a call can reach. Read the two REFUSALS first
— they are behaviour a buyer-facing doc has to describe, because an ordinary
selection can now be declined where it previously returned mail.

### Added — survey_inbox, a read-only triage worklist

A ninth tool. It reads the whole inbox in one pass and returns the mail
together with the taxonomy's folder DESCRIPTIONS, so a classifier has the
decision criteria in the same response as the messages it must decide about.

**It is read only.** `survey_inbox` calls `list_inbox` and nothing else. It
classifies nothing, moves nothing, and writes nothing — including no ledger
row, because a tool that changes nothing has nothing to receipt. Acting on the
worklist still goes through `file_message` and `mark_important`, which remain
the only tools that touch the mailbox and which receipt themselves.

Why it is not just a bigger `list_inbox`: the difference is the taxonomy, not
the page size. `list_inbox` reports folder NAMES, enough to file one message
already read. Deciding where a hundred messages go, consistently, in one pass
needs the criteria that separate the folders, and those live in the
descriptions. Sending them WITH the mail is what stops the criteria being
recalled from memory halfway down a long worklist.

- `limit` defaults to 100 (against `list_inbox`'s 50) and is CLAMPED to 1-200
  rather than refused, at `MAX_SURVEY_PAGE` — a separate constant from
  `MAX_INBOX_PAGE` on purpose, so a later decision about how much a sweep may
  see does not have to move the plain listing with it.
- `truncated` is reported explicitly rather than left to be inferred from
  `count == limit`. A sweep that silently stopped short leaves the owner
  believing their inbox was triaged when part of it was never looked at.
- The payload carries a `read_only` statement and the standard
  `untrusted_content_warning`. Snippets are attacker-controlled text like any
  thread body, and nothing read there can widen the folder set — `file_message`
  validates against the closed taxonomy regardless.

Docs updated for the count: `README.md` (tool table plus a section on why the
criteria ship with the mail) and `docs/STANDARDS-COMPLIANCE.md`, both of which
still claimed eight tools.

### Changed — read_sent_sample is SENT-ONLY IN CODE, on both providers

Previously the sent-only contract was a property of the SEARCH QUERY: Gmail's
`in:sent` and Graph's `sentitems`-scoped collection URL. The hand-picked
`message_ids` path never touched either, so an id from `list_inbox` returned a
RECEIVED message's full body from a tool whose contract says sent mail only.
The guarantee now belongs to the fetch, which no selection path can avoid.

- **Gmail** verifies the `SENT` label at the single fetch both selection paths
  pass through, using `labelIds` already present on the `format="full"`
  response — the proof costs no additional request.
- **Graph** compares each message's `parentFolderId` against the resolved
  `sentitems` folder id, because `GET /me/messages/{id}` is mailbox-wide and
  carries no folder scoping of its own.

**Both REFUSE THE WHOLE SELECTION rather than dropping the offending message.**
This is a deliberate behaviour change and it is the one most likely to surprise
a caller: a short list reads to the calling model as "there was not much
there", so a silent drop would hide that the request asked for mail this tool
does not return. A selection of twenty ids where one is a received message now
returns nothing and names the problem.

Consequence for callers, and for any workflow written against the old shape:
**a selection built from `get_thread` message ids will normally be refused**,
because a thread contains received messages as well as sent ones. Selections
that succeed are labels, a date range, or ids known to be sent mail.

### Changed — exclusions no longer "win" over a non-sent message

Supersedes the 0.2.0 line "Exclusions win over inclusions on every path,
including the message-id path". Exclusions still win over INCLUSIONS, and that
is unchanged. But an id outside the sent folder is no longer something an
exclusion can quietly absorb: Gmail checks `SENT` BEFORE applying exclusions,
so a non-sent message is reported as non-sent rather than being swallowed by an
exclusion that happens to also match it. Narrowing what the owner is entitled
to read, and reaching for mail the tool does not sample, are different events
and now report differently.

### Added — a date bound that is not a date is REFUSED before any request

`after` and `before` are MCP tool parameters that the tool layer only strips.
Both adapters now validate them, and both refuse rather than clean:

- **Graph** requires exactly `YYYY-MM-DD`. OData dates are UNQUOTED, so there
  is no quote to double and no escaping that makes a malformed bound inert —
  the shape check is the entire defence. `2026-01-01 or id ne null` would
  otherwise widen the filter back into the whole-folder read the tool exists to
  refuse.
- **Gmail** parses with `date.fromisoformat` and re-emits from the PARSED
  object, so nothing of the input string survives into the query. The
  predecessor was a `-` → `/` substitution on an unexamined string.

This refusal is reachable from ordinary use: a caller passing `01/31/2026`, a
month name, or an empty-ish placeholder gets a refusal naming the required
form, not a widened search.

### Changed — request construction takes segments, not formatted strings

Round-1 guards were opt-in per call site, so a path not on the list bypassed
them. Each is now structural:

- **Graph paths.** The HTTP helpers take path SEGMENTS, and one boundary
  validates every non-static segment against the id allowlist before encoding.
  Public methods no longer call the validator themselves, so a new call site
  cannot omit it. An AST test fails the build if any dynamic value is
  interpolated into a path.
- **Graph OData filters.** One builder produces every filter value, validating
  thread ids against the SAME allowlist used for path segments rather than a
  second grammar that could drift. Previously a crafted thread id could widen
  the filter to select unrelated messages — and since the thread projection
  includes bodies, `get_thread` could return bodies from outside the requested
  conversation while `create_draft` could anchor a reply to an unrelated
  message. The structural scan that refuses interpolated paths now refuses
  interpolated filters too.
- **Gmail query clauses.** Clause templates and their validators live in one
  table, and the only formatter looks the validator up by the same key, so a
  clause cannot render without one. A row without a validator is a `KeyError`
  in the first test that compiles a query, not a widened query in production.
- Gmail label terms carrying query syntax (a quote, a bare boolean, a search
  operator prefix) are REFUSED rather than escaped — Gmail publishes no escape
  for a quote inside a quoted term, so declining the input is the only sound
  answer.

### Changed — logging

- Mail content is kept out of the rendered message string by construction, and
  an AST guard now considers EVERY argument that can reach a rendered message.
  The previous guard read only the first argument, so Loguru's own formatting
  API carried content past it.
- The defensive redaction pass handles multi-word values; the previous regex
  rewrote only the first token.
- Upstream error excerpts no longer carry request content into logged
  exceptions.

### Fixed

- Graph retryable statuses now actually retry, on READS only (`httpx` does not
  raise on 4xx/5xx, so the previous wiring never retried). Mutations still
  never retry, preserving the ledger's 1:1 invariant.
- Graph `file_message` records the post-move id, so receipts resolve.
- Owner-resolution failure surfaces as an actionable auth error instead of an
  opaque SDK exception; the catch is the google-auth tree BASE, so a revoked
  refresh token degrades as documented rather than escaping as a traceback.
- A self-addressed `Reply-To` no longer hard-refuses a thread: owner stripping
  happens before recipient preference is resolved, so it falls back to the
  `From` correspondent.
- A malformed ledger row is skipped as documented instead of raising out of the
  whole digest.
- The Gmail draft's own headers match the recipients the tool reports.
- Copied reply headers are sanitized, so hostile header content cannot raise
  out of draft creation.
- The drafts posture can no longer be changed by an environment variable behind
  the recorded config.
- `FiledMessage` survives copy and pickle.

### Fixed — the Graph sent-only guard could fail OPEN on absent evidence

The sent-mail proof compared two strings that were each allowed to be empty:
`str(item.get("parentFolderId") or "")` against a `_sent_folder_id()` that
returned `str(folder.get("id") or "")`. **Both halves degrade to the empty
string under the SAME condition — absent evidence — so when the `sentitems`
resolve produced no usable id AND a message arrived without a
`parentFolderId`, the comparison was `"" == ""`, the guard passed, and
`read_sent_sample` returned the full body of an inbox message.** The guard was
not weak; it was inverted by emptiness.

This also contradicted the code's own docstrings, which claimed a message
arriving without the field "is the same thing evidentially" and "is REFUSED",
and that dropping the field from the projection "would make every message look
unparented and refuse the whole path". Neither held.

Fixed as a TYPE rather than a comparison patch: `ResolvedId` cannot be
constructed empty, so a comparison against one has no degenerate form to reach
and the failure mode is removed from the vocabulary instead of being guarded at
each use. It lives in a new `providers/graph_evidence.py` because `graph.py` is
at its code-line ceiling and compressing working code to make room is what
caused a regression the last time it was tried.

Absent evidence now REFUSES on both sides of the comparison, which is what the
buyer-facing refusal lists have always described.

### Verification

452 tracked tests pass, plus `ruff`, `mypy --strict`, and the size check. Still
UNVERIFIED: anything requiring a real tenant — the DWD grant, whether Workspace
per-app access control blocks send for this app, and real Gmail/Graph draft
creation. Every adapter behaviour above is proven against a mocked transport
only.

## 0.2.0 — 2026-08-17

Stage 2 (voice + reply drafts). **WRITTEN and locally tested; not deployed,
not dogfooded, not shipped.** MINOR rather than PATCH because the tool surface,
the provider interface, and the Gmail scope posture all changed.

> **Superseded in part by Unreleased.** The `read_sent_sample` contract below
> describes the round-1 shape. Sent-only is now enforced in code on every
> selection path, a non-sent message id refuses the whole selection, and an
> invalid date bound is refused before any request. The line "Exclusions win
> over inclusions on every path, including the message-id path" no longer
> describes how a non-sent id is handled — see Unreleased.

### Added

- `read_sent_sample` tool and provider method. Takes an EXPLICIT selection
  (labels/categories, date range, message ids, plus exclusions) and reads only
  that. An empty selection is refused at the tool layer and again in each
  adapter; there is no argument-free path that reads a whole sent folder.
  Exclusions win over inclusions on every path, including the message-id path
  that bypasses the search query.
- `create_draft` tool and provider method. Reply drafts on existing threads,
  never sent. **No recipient parameter exists** at the tool layer, on the
  interface, or on either adapter — recipients are the thread's participants
  minus the owner, derived from the provider's view of the thread.
  Constraints enforced before any API call: thread must exist and have a
  participant other than the owner, body non-empty and under
  `MAX_DRAFT_BODY_CHARS` (8000), and a caller-supplied `voice` validated
  against the installed list.
- `get_voice_routing` tool. Reports the rules and the LIVE no-send posture
  computed from the installed config, so the host AI states the true posture
  instead of reciting a claim that may not hold for this deployment.
- `config.py` — installer-owned configuration: per-provider `drafts_enabled`
  and the voice routing table. No MCP tool writes it, so the AI can neither
  enable drafts nor rewrite the rules it is subject to. Absent config loads a
  safe default (drafts off, no routing), which is strictly narrower than any
  configured state.
- `resolve_voice` — pure function, no I/O and no message bodies. Rules:
  all-recipient-domains == owner domain → internal; configured sales label →
  sales; otherwise external. Ambiguity (no recipients, unparseable recipients,
  a mixed internal/external thread) defaults to external. Domain matching is
  exact, so `mail.example.com` and `evil-example.com` are external.
- `Thread.participants` / `Thread.labels`, plus `SentMessage`,
  `SentSelection`, and `DraftResult`. `get_thread` now also returns
  `reply_would_go_to` and the routed `voice`.
- Ledger schema 2: an optional `voice` field, additive, recording which voice
  wrote a draft. v1 rows read back unchanged with `voice=""` — the ledger is
  appended to, never migrated. The daily digest breaks drafts down by voice.
- `init-config` CLI command; `show-scopes` and `auth` now report the live,
  config-dependent posture rather than a fixed table.

### Changed — SECURITY POSTURE (read this one)

- **Gmail with drafts enabled drops from scope-layer to code-layer.** This is
  Google's design, not a defect and not a shortcut: `users.drafts.create` and
  `users.drafts.send` accept an IDENTICAL scope set, so no
  compose-without-send scope exists. `NO_SEND_ENFORCEMENT` is now computed by
  `scopes.enforcement_for(provider, drafts_enabled)` rather than hardcoded,
  because a constant would go stale the first time somebody flipped the flag.
  Stage 1 predicted this exact downgrade; it happened as written.
- **`drafts_enabled` lets an installer decline it.** With Google drafts off,
  the compose scope is never requested — not merely unused — so the buyer can
  verify the Stage 1 posture from their own admin console.
- **Microsoft is UNCHANGED.** `Mail.ReadWrite` already covers `createReply`
  and is refused by both send paths, so Graph keeps the scope-layer guarantee
  with drafts on. The asymmetry is real; buyer copy must not flatten it.
- `scopes.scopes_for` gained a `drafts_enabled` argument and now raises
  `ValueError` (was `KeyError`) on an unknown provider.
- `MailProvider` grew from five operations to seven. `GMAIL_SCOPES` was
  replaced by `GMAIL_TRIAGE_SCOPES` plus `GMAIL_COMPOSE`.

### Notes / deviations

- **`GMAIL_COMPOSE` is defined by indexing into `GMAIL_SEND_WOULD_REQUIRE`**
  rather than by writing the literal a second time. The scope that creates a
  Gmail draft genuinely IS a member of the send-capable set; an index states
  that and cannot drift, where a copy could. Side effect: enabling drafts adds
  no send-scope literal to the source, so both scanners stay strict.
- **The Gmail adapter reaches its one drafts call by name**
  (`_DRAFTS_RESOURCE` / `_CREATE_METHOD`) instead of `drafts().create(...)`.
  Gmail's create and send methods differ only in the final word, so a scanner
  permitting `drafts.create` would be one typo from permitting `drafts.send`.
  One reviewable indirection buys a scanner that cannot be loosened; the call
  is named in the module docstring, the README, and two tests.
- `createReply` was REMOVED from the forbidden-endpoint list — it is Graph's
  reply-DRAFT action and does not send. Narrowed rather than dropped:
  `test_graph_only_reaches_the_reply_draft_action` asserts the literal appears
  exactly once, in one named constant. `sendMail`, `/send`, and
  `createForward` remain banned.
- `create_draft` was removed from `FORBIDDEN_ATTRS` (it is now the feature) and
  `send_draft` added in its place.
- Graph applies category filters client-side because Graph's `categories`
  collection is not reliably filterable alongside the date clauses in one
  `$filter`. Date bounds and message ids still go server-side, so the read is
  bounded before it leaves Microsoft. A performance difference, not a security
  one — UNVERIFIED against a live tenant, like everything else here.
- `_creds_to_payload` now records the scopes Google actually granted instead of
  falling back to the locally requested set, so a grant that differs from the
  request cannot be stored as though it matched.
- Verified live: MCP stdio handshake reports version 0.2.0 and exactly eight
  tools; `create_draft`'s advertised schema carries only `thread_id`, `body`,
  and `voice`; an empty `read_sent_sample` call is refused over the wire.
  `_graph/done-means/inbox-zero-no-send.sh` still exits 0 against the Stage 2
  tree, unmodified.
- 223 tests (was 84). Not verified: anything requiring a real tenant — the DWD
  grant, whether Workspace per-app access control actually blocks send for this
  app, and real Gmail/Graph draft creation.

## 0.1.0 — 2026-08-17

Stage 1 (triage) initial build. **WRITTEN and locally tested; not deployed,
not dogfooded, not shipped.** Per the kit spec (D3) the kit ships only after
Stage 2 lands.

### Added

- uv app scaffold: `pyproject.toml` (`requires-python >=3.13`), `.python-version`,
  `uv.lock`, `requirements.txt` exported from the lock, src layout, hatchling
  build, `inbox-zero-connector` entrypoint.
- `scopes.py` — the single security manifest. Gmail
  `{gmail.readonly, gmail.modify.restricted}` and Graph `{Mail.ReadWrite}`,
  each with its doc citation and what sending would additionally require.
- `providers/base.py` — five-operation interface. No send, draft, delete, or
  forward methods; the absence is documented as the security boundary.
- `providers/gmail.py` — read/label/flag via `messages.list|get|modify` and
  `labels.list|create`. Labels applied message-by-message because
  `threads.modify` does not accept `gmail.modify.restricted`.
- `providers/graph.py` — read/move/flag via `/me/messages`, `/me/mailFolders`,
  `/move`, and `PATCH`. No `DELETE` verb is ever issued.
- `taxonomy.py` — closed folder vocabulary loaded from the config dir;
  `validate` rejects anything outside it, bounding prompt-injection blast
  radius to a misfile.
- `ledger.py` — append-only JSONL audit ledger with `fsync`, plus the daily
  digest renderer. Records actions only, never mail content.
- `server.py` — MCP stdio server exposing exactly five tools. Deliberately no
  `write_ledger` tool; receipts are a side effect of a mutation succeeding.
- `auth.py` — OS-keychain-only token storage via `keyring`. No token is ever
  written to a file or a log.
- `cli.py` — `serve`, `auth`, `init-taxonomy`, `show-scopes`, `paths`,
  `digest`, `logout`.
- 84 pytest tests covering taxonomy, ledger, both adapters against mocked
  transports, the tool layer, and `test_security_surface.py`.

### Notes / deviations

- **Gmail scopes differ from the kit SPEC.md table.** SPEC listed
  `gmail.modify`; that scope IS accepted by `users.messages.send`, so it would
  have refuted the headline claim. Stage 1 excludes drafts, which frees the
  true no-send pair `{gmail.readonly, gmail.modify.restricted}` and makes the
  claim scope-layer true on both providers. SPEC's "verify at build" gate is
  what caught this.
- Dependency `mcp` pinned `>=2.0,<3`: v2 relocated the server class from
  `mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer`.
- Verified live: MCP stdio handshake lists exactly the five tools and a tool
  call round-trips; the server refuses to start without an installed taxonomy.
