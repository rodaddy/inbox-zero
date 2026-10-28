# Standards compliance receipt — inbox-zero connector

**Status: MERGED-pending. Every gate result below was RUN on 2026-08-17 against
this working tree, on the branch `the kit branch`, and is quoted verbatim.
Nothing here is deployed.** The connector has not been installed against a real
tenant; these are source, lint, type, and test results, not production evidence.

This document is for the buyer's security auditor. It maps each applicable LAW
and rule from the three synced standards documents to the exact place this
connector satisfies it, or records an explicit N/A with the reason. It is
deliberately written to be *checkable*: every row names a file and line, a
config key, or a command you can run yourself.

Standards under audit, as synced into this repo:

| Document | Covers |
| --- | --- |
| `_DOCS/STANDARDS-python.md` | toolchain, config keystone, typing, control flow, size, testing, error handling |
| `_DOCS/STANDARDS-observability.md` | logging, redaction, the five points, retries |
| `_DOCS/STANDARDS-core.md` | control flow, minimal correct change, comments, verification, secrets |

---

## 0. How to reproduce every claim in this document

```bash
cd kits/inbox-zero/connector
uv run pytest                                   # 290 passed
uv run ruff check .                             # All checks passed!
uv run mypy src                                 # Success: no issues found in 16 source files
uv run python scripts/check_code_size.py        # Size OK (16 file(s) examined) — exit 0
../../../../_graph/done-means/inbox-zero-no-send.sh   # RESULT: PASS — EXIT=0
```

Results observed 2026-08-17, in that order, all green. The suite grew from 223
to 290 tests during this compliance pass; the 67 added are the enforcement and
redaction tests described below, not restatements of existing behaviour.

**Tool behaviour is unchanged by this pass.** The MCP surface is the same eight
tools with the same wire schemas, `create_draft` still has no recipient
parameter, and the no-send check exits 0 exactly as it did before.

**Since this pass**, a ninth tool was added: `survey_inbox`, a read-only triage
worklist that calls `list_inbox` and ships the taxonomy's folder descriptions
alongside the mail. It writes nothing — no mutation, no ledger row — so the
compliance findings recorded below are unaffected by it. A tenth,
`record_correction`, was added after that: it files a message and writes a
learned rule, so unlike `survey_inbox` it IS mutating and IS receipted (two
rows, one per mutation). Its body lives in `corrections.py` because `server.py`
was at 461 of the 500 code-line ceiling. The current surface is ten tools.

---

## 1. `_DOCS/STANDARDS-python.md`

### Toolchain (fixed, not per-project choices)

| Concern | Required | Where satisfied |
| --- | --- | --- |
| Python 3.14 minimum | `requires-python`, ruff `target-version`, mypy `python_version` — all three | `pyproject.toml` `requires-python = ">=3.14"`; `[tool.ruff] target-version = "py314"`; `[tool.mypy] python_version = "3.14"`; `.python-version` |
| Interpreter + deps | `uv` | `uv.lock` committed; every command above is `uv run` |
| Format + lint | `ruff` | `[tool.ruff.lint] select` — 27 rule selectors, `pyproject.toml` |
| Types | `mypy` strict | `[tool.mypy] strict = true` plus `warn_unreachable`, `warn_unused_configs`, `warn_redundant_casts` |
| Data + config | `pydantic` v2 | 15 models; **zero** `@dataclass` remain (see §1.2) |
| Logging | `loguru` | `src/inbox_zero_connector/utils/logging_config.py` |
| Tests | `pytest` | `[tool.pytest.ini_options]`, 290 tests |

`requirements.txt` is generated from the lock so the connector also runs without
uv (repo ruling 2026-08-11).

### LAW: `uv run` for everything

**SATISFIED.** No command in this package's docs, tests, or scripts activates a
venv or calls a bare interpreter. `scripts/check_code_size.py` is invoked as
`uv run python scripts/check_code_size.py`, and where a test shells out it uses
`sys.executable` (`tests/test_standards_enforcement.py:116`), never a bare name.

### LAW: do not hand-roll a solved problem

**SATISFIED, and it changed the dependency list.** Audited for the four jobs the
standard names — retry/backoff, config loading, HTTP, logging:

| Job | Before this pass | Now | Where |
| --- | --- | --- | --- |
| Retry / backoff | **absent entirely** — no bounded retry on any outbound call | `tenacity` (`stop_after_attempt` + `wait_exponential_jitter` + `retry_if_exception`) | `providers/retry.py:115-122` |
| Layered config load | hand-rolled: `json.loads` + hand-written `_parse_*` validators + manual precedence | `pydantic-settings` `JsonConfigSettingsSource` declared in `settings_customise_sources` | `config.py:513-556` |
| Logging sinks / rotation / retention / JSON | **absent entirely** — no logging of any kind | `loguru` | `logging_config.py:258-334` |
| HTTP client | already `httpx` (Graph) and the vendor SDK (Gmail) | unchanged | `providers/graph.py`, `providers/gmail.py` |
| Validation | hand-written type/shape checks raising `ConfigError` | pydantic validators; the message text is preserved so operator-facing errors did not regress | `config.py:208-330` |

The retry policy keeps only the part that is genuinely domain knowledge — WHICH
failures deserve another attempt (`RETRYABLE_STATUS`, `providers/retry.py:52`) —
and deliberately excludes 401/403, because an auth or scope refusal is a
configuration fact and retrying it would make a connector that hammers a
send-refusal look exactly like one trying to get around it.

**Mutations are deliberately NOT retried** (`providers/retry.py` module
docstring; `providers/gmail.py:399`, `providers/graph.py:206,218`). A
`file_message` that timed out may already have applied the label, and a blind
second attempt would produce a second ledger row for one real change — which
would break the "action count == ledger row count" invariant this kit is
audited on.

### LAW: Pydantic for every model. No bare dataclasses.

**SATISFIED.** All six `@dataclass(frozen=True)` declarations were converted.
`rg 'dataclass' src/` now returns only two prose mentions inside docstrings.

| Model | File:line | Config |
| --- | --- | --- |
| `Section` (base for settings sections) | `config.py:166` | `extra="forbid"`, `frozen=True` |
| `LoggingSettings` | `config.py:179` | inherits `Section` |
| `VoiceRouting` | `config.py:208` | inherits `Section` |
| `ConnectorConfig` | `config.py:278` | inherits `Section` |
| `CredentialSettings` | `config.py:426` | inherits `Section` |
| `Settings` (`BaseSettings`) | `config.py:469` | `extra="forbid"`, `validate_assignment=True` |
| `RoutingInputs` | `config.py:690` | `extra="forbid"`, `frozen=True` |
| `ProviderModel` (base) | `providers/base.py:87` | `extra="forbid"`, `frozen=True` |
| `Message` / `Thread` / `SentMessage` / `SentSelection` / `DraftResult` | `providers/base.py:100,117,140,157,194` | inherit `ProviderModel` |
| `Taxonomy` | `taxonomy.py:80` | `extra="forbid"`, `frozen=True` |
| `LedgerEntry` | `ledger.py:59` | `extra="forbid"`, `frozen=True` |

**`extra="forbid"` is a security control here, not tidiness.** A misspelled key
is otherwise dropped silently and the default used — and one of these settings
decides whether a send-capable Gmail scope is requested.

**AUDITOR: read this one.** `ConnectorConfig.drafts_enabled` is typed
`dict[str, StrictBool]` with a `mode="before"` validator that refuses any
non-boolean (`config.py:286-313`). Measured 2026-08-17 against this package's
own model: pydantic's *ordinary* `bool` coerces `"yes"` and `"1"` to `True`.
Since `drafts_enabled.google = true` is what makes the connector request
`gmail.compose` — a scope Gmail's send methods also accept — an ordinary `bool`
would let a config holding `"yes"` silently move a deployment from a
scope-layer no-send guarantee to a code-layer one, with no error anywhere.
Test: `tests/test_config_routing.py::test_a_truthy_string_does_not_enable_drafts`.

### LAW: `config.py` is the keystone — nothing else reads the environment

**SATISFIED, and mechanically enforced.** Environment access was previously
spread across three modules (`paths.py`, `server.py`, `auth.py`); it now exists
in exactly two lines, both in the keystone:

- `config.py:381` — `INBOX_ZERO_CONFIG_DIR`
- `config.py:390` — `INBOX_ZERO_DATA_DIR`

Everything else reaches the environment through typed `Settings` fields:
`INBOX_ZERO_PROVIDER` → `Settings.provider` (a `Literal`, so an invalid value
fails at startup naming the field, not at the first tool call);
`INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_ID` / `_SECRET` / `INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID` /
`_TENANT_ID` → `CredentialSettings` (`config.py:426`).

`paths.py` is now a re-export shim over the keystone (`paths.py:1-40`) so the
boundary moved without every caller moving with it.

**The mechanism:** `tests/test_standards_enforcement.py::test_only_the_config_keystone_reads_the_environment`
walks the AST of every module and fails on any `os.environ` / `os.getenv` /
bare `getenv` outside `config.py`. **RED-verified 2026-08-17**: planting
`os.environ.get("LEDGER_DEBUG", "")` in `ledger.py` produced
`FAILED ...test_only_the_config_keystone_reads_the_environment[ledger.py]`;
removing it returned the suite to green. The same test asserts `config.py`
*does* read the environment, so a check that examined nothing cannot pass.

### LAW: JSON config layers loaded by `pydantic-settings`. Not by you.

**SATISFIED.** `Settings.settings_customise_sources` (`config.py:513-556`)
returns the source chain, so the precedence order is a property of the
mechanism rather than a claim in a docstring:

1. init kwargs (tests only)
2. `INBOX_ZERO_*` environment
3. `connector-config.json` — the installer's decisions

The installer file is the **lowest** layer deliberately: an operator's
environment override must win over a stale installed file, never the reverse.
A missing file is skipped rather than fatal, because absence means the Stage 1
posture (drafts off, no routing), which is strictly narrower than any
configured state.

Invalid JSON and invalid values are distinguished (`config.py:566-585`), because
"the file is not JSON" and "the file is JSON but says something wrong" have
different fixes and one message covering both sends the operator to the wrong
one.

### Fail fast, with the remediation in the message

**SATISFIED.** Every raise states what broke, where, and what to do. Grep
`ACTION REQUIRED` across `src/` — it appears in `config.py` (draft posture,
routing absent, invalid JSON, invalid voice list, unknown provider), `auth.py`
(all four keychain/credential paths), `providers/gmail.py`, `providers/graph.py`,
and `server.py`.

This is why ruff's `TRY003` (`raise-vanilla-args`) is in `ignore` with a stated
reason (`pyproject.toml` `[tool.ruff.lint] ignore`): the rule bans long messages
inside `raise`, and this package deliberately does the opposite. These messages
are read by a host AI that has no other channel to learn the remediation.

### LAW: secrets are fetched, and the NAME is committed config

**SATISFIED, adapted.** The connector holds no rTech-owned credential at all.

- **User tokens** never touch disk: OS keychain only, via `keyring`
  (`auth.py`). There is no file fallback, by design, and the error says so.
- **OAuth client identifiers** are the *buyer's own* app registration in the
  *buyer's own* tenant. Only the variable NAMES are committed
  (`auth.py:33-42`); values come from the environment through the keystone.
- No error message in `auth.py` interpolates a token value.

**N/A — `vaultwarden-secrets`.** The standard's fetch-from-vault mechanism is
for rTech-operated services. This connector runs on the *buyer's* machine
against the *buyer's* tenant, and pointing it at an rTech credential store would
directly contradict the vendor-agnostic doctrine (2026-08-02) that the kit is
sold on: their tenant, their keys, rTech absent from the token chain. The OS
keychain is the correct equivalent and is strictly better here — it is not
network-reachable.

### LAW: tight, single-purpose code — the size limits

**SATISFIED, with its own checker, because ruff has no line-count rule at all.**

`scripts/check_code_size.py` parses each file with `ast` + `tokenize`, counts
CODE lines only (blanks, comment-only lines, and docstrings excluded), and
enforces 500/file and 50/function. Current result: `Size OK (16 file(s)
examined)`, exit 0.

The docstring exclusion is load-bearing *for this package specifically*: the
connector is deliberately docstring-heavy because the docstrings are the
security argument you are reading them for. Counting them would push every
module over a ceiling that exists to catch logic sprawl, and the cheapest way
to go green would be deleting the explanations — exactly backwards.

**RED-verified 2026-08-17**, in the suite itself, not just by hand:

- `tests/test_standards_enforcement.py::test_the_size_check_rejects_an_oversized_function`
  feeds it a 61-line function and asserts exit 1 plus `bloated()` in the output.
- `...::test_the_size_check_ignores_comments_and_docstrings` feeds it a function
  padded with 200 comment lines and asserts exit 0.

The standard records the exact failure this guards against: a size checker whose
path constant went stale after a move printed `Size OK` on every run because it
examined an empty directory, and nothing noticed. `PACKAGE_ROOT` here is
existence-checked and returns **exit 3** (harness error) rather than a false
green (`scripts/check_code_size.py:150-157`).

One real breach was found and fixed by this checker during the pass:
`cli.py::build_parser` was 54 code lines; `_add_init_config_parser` was split out
(`cli.py:216`).

Ruff's statement/argument legs are configured per the standard's exact numbers:
`max-statements = 25`, `max-args = 5`, `max-nested-blocks = 3`,
`max-branches = 12`, `max-complexity = 10`.

### LAW: mypy strict passes, with no blanket `--ignore-missing-imports`

**SATISFIED.** `uv run mypy src` → *Success: no issues found in 16 source
files*. `strict = true`, plus `warn_unreachable`, `warn_unused_configs`,
`warn_redundant_casts`, and `enable_error_code = ["ignore-without-code",
"redundant-expr", "truthy-bool"]` — so every `# type: ignore` must name the
error it silences and a stale one becomes an error rather than quietly covering
a new problem.

The only `ignore_missing_imports` is a **named per-module override** for the
five vendor packages that genuinely ship no stubs (`googleapiclient.*`,
`google_auth_oauthlib.*`, `google.oauth2.*`, `google.auth.*`, `msal.*`).

There is exactly **one** `# type: ignore` in `src/`: `ledger.py:92`, coded
`[assignment]`, with five lines of comment explaining that `schema` is the
on-disk key on every v1 row of a real append-only ledger and renaming it would
strand those receipts.

Absolute imports everywhere: relative imports (`from .x import`) were replaced
throughout `providers/`, `server.py`, `cli.py`, `auth.py`, `ledger.py`, and
`taxonomy.py` during this pass.

### LAW: flat control flow

**SATISFIED, and the enforcement is verified live.** `preview = true` is set
(`pyproject.toml:55`) — without it ruff prints *"Selection PLR1702 has no effect
because preview is not enabled"* and exits 0, which is a check reporting success
while examining nothing.

**Verified fires, 2026-08-17.** A probe with genuinely non-collapsible 4-deep
nesting produced `too-many-nested-blocks: Too many nested blocks (4 > 3)`. A
broader probe confirmed `bare-except`, `call-datetime-now-without-tzinfo`,
`commented-out-code`, `debugger`, `print`, `too-many-arguments`,
`try-except-pass`, and `suppressible-exception` all fire.

Rule tables in place of conditionals — the standard's preferred shape:

| Table | File:line | What it replaced |
| --- | --- | --- |
| `VOICE_RULES` — predicate/role rows | `config.py:726` | the voice-routing conditionals |
| CLI `handlers` dict dispatch | `cli.py:_dispatch` | an 8-arm if/elif ladder in `main()` |
| Graph mock route table | `tests/test_graph_provider.py:92` | a 9-arm handler ladder (was `complex-structure: 11 > 10`) |
| `SENSITIVE_FIELD_PATTERNS` / `ID_FIELD_SUFFIXES` | `logging_config.py:130,175` | per-call-site redaction decisions |
| `RETRYABLE_STATUS` | `providers/retry.py:52` | (new) |
| Taxonomy closed vocabulary | `taxonomy.py:26` | (pre-existing, verified table-shaped) |

**Verified as required by the task:** the taxonomy and the voice routing were
both already table-driven in substance. The voice routing was refactored from a
sequential-`if` shape into an explicit predicate table (`VOICE_RULES`) whose last
row matches everything, so routing is **total by construction** and there is no
fall-through path to reason about. `RoutingInputs` (`config.py:690`) is
`extra="forbid"` and has **no body or subject field at all** — a predicate
cannot consult content it was never handed, which is why no prompt-injected
instruction in an email can reach the voice decision.

### LAW: test every input and output, not a coverage percentage

**SATISFIED.** 290 tests. **No coverage gate is configured**, deliberately — the
standard forbids one, and `pyproject.toml` contains no coverage config.

Tests added during this pass, each proven able to fail:

| Test file | Proves |
| --- | --- |
| `tests/test_logging_redaction.py` (31 tests) | no log line contains a body, subject, address, or token |
| `tests/test_standards_enforcement.py` (36 tests) | keystone, stdout, and size rules hold |

`filterwarnings = ["error"]` (`pyproject.toml`) — a deprecation cannot
accumulate unnoticed the way it does when a suite prints warnings and stays
green. One documented exemption, for the deliberate `LedgerEntry.schema` name.

### LAW: fail loudly, or log enough to find it

**SATISFIED.** `BLE` (blind-except) is selected and passes. Every `except` names
its types. Two fail-open paths exist and both are documented decisions that log
before continuing, per the standard's requirement that an intentional fail-open
be a documented decision in the code:

- `server.py:172` — owner-address resolution catches
  `(ProviderError, ConfigError, OSError)`, logs at WARNING with the concrete
  impact, and continues. The direction is chosen: an unknown owner means no
  recipient is dropped (harmless — mail clients de-duplicate), where dropping
  the *wrong* address would send a draft to fewer people than the thread had.
  This replaced a bare `except Exception: # noqa: BLE001` that logged nothing.
- `ledger.py:152,162` — a malformed ledger row is skipped and the skip is
  **announced** (line number only, never content). Previously a silent
  `continue`, which meant a digest could under-report what was done to the
  mailbox — the one thing that file exists to prevent.

`raise ... from` on all 7 re-raises. `ledger.append` re-raises `OSError` after
logging (`ledger.py:119-128`): a mutation without its receipt breaks the audit
invariant, so it is fatal to the call, never swallowed.

### Documentation

**SATISFIED.** Every `__init__.py` carries a full module docstring;
`providers/__init__.py` was rewritten to the standard's template (Key
Components / Pattern / Example / See Also) and its `get_provider` logic moved to
`providers/factory.py`, because an `__init__.py` holds the docstring and the
re-exports, not logic.

**N/A — `generate_folder_docs.py` / generated `README.md` per folder.** This
package has no `_githooks/` of its own (see §4) and its README is hand-authored
buyer-facing documentation, not a generated artifact. Adding a generator that
overwrote it would destroy the security narrative the buyer reads. The
docstrings are still the qmd retrieval surface and are written first.

---

## 2. `_DOCS/STANDARDS-observability.md`

### Every outcome emits a signal

**SATISFIED.** Absence of a log line is never proof of success: every operation
emits its exit/result line, and every degraded, fallback, skipped, or
guard-triggered path emits a warning.

Before this pass the connector had **no logging at all** — every rule in this
section was unmet, not partially met.

### Log at five points, preferring a wrapper

**SATISFIED, via a wrapper, exactly as the standard advises** ("prefer a wrapper
that emits entry/exit/duration/failure automatically over asking each call site
to remember five calls").

`logging_config._FivePointCall` (`logging_config.py:378`) with two subclasses:

| Point | Level | Emitted by |
| --- | --- | --- |
| Entry | `debug` | `__enter__` — automatic |
| Exit / result + duration | `info` | `__exit__` — automatic |
| Failure | `error`, **always `exc_info=True`** | `__exit__` — automatic |
| Retry | `warning`, with attempt number | `.retry()`, called by `providers/retry.py` `before_sleep` |
| Fallback / degraded | `warning`, with reason | `.fallback()` |
| Guard / limit | `warning`, with requested vs applied | `.guard()` |

- `ProviderCall` (`logging_config.py:451`) wraps **every outbound provider
  call** — 8 sites in `gmail.py`, 4 in `graph.py` (Graph's are on `_get`/`_post`/
  `_patch`, through which every Graph request funnels, so no future method can
  forget).
- `ToolCall` (`logging_config.py:469`) wraps **all 8 MCP tool invocations** in
  `server.py`.

`__exit__` returns `Literal[False]`, not `bool` — stating in the signature that
this wrapper observes and never swallows, which is the rule this document is
most emphatic about. (It also tells mypy the block cannot suppress, so a
`return` inside a wrapped body is reachable.)

Every line carries a `correlation_id` bound for the whole span
(`logging_config.py:391`), so a tool invocation and the provider calls it made
stitch back together out of interleaved work.

### Structured output and required fields

**SATISFIED.** Three sinks, configured once, at startup, from `LoggingSettings`
— and **nowhere else**:

| Sink | Destination | File:line |
| --- | --- | --- |
| Console | **`sys.stderr`** | `logging_config.py:282-283` |
| Rotating plain text | `{data_dir}/logs/inbox-zero-connector-{env}.log` | `logging_config.py:308` |
| Structured JSON, one object per line | `...-{env}.jsonl`, `serialize=True` | `logging_config.py:325` |

Rotation **1 MB**, retention **3 files** (`config.py:198,201`) — the standard's
exact numbers. Log level comes from configuration and an invalid value fails at
boot: `LoggingSettings.level` is a `Literal[...]`, so `LOUD` is a startup
validation error naming the field, not a silent default.

Every record carries `service` and `correlation_id` (`logging_config.py:272`).

**AUDITOR: THE STDERR CHOICE IS A CORRECTNESS REQUIREMENT, NOT A PREFERENCE.**
This connector speaks MCP over stdio — JSON-RPC frames on **stdout**, read by
the host AI's transport. One stray byte there lands mid-frame and desynchronises
the transport, killing the connector mid-conversation with a parse error naming
the log line rather than the code that emitted it. So:

1. The console sink is `sys.stderr`, unconditionally, with no setting that moves
   it. Stated in a comment at the sink and in a banner section of the module
   docstring (`logging_config.py:19-33`).
2. ruff's `T20` (`print`) is selected **package-wide**, with narrow per-file
   exemptions only for `cli.py` (whose product *is* its stdout) and
   `graph.py` (the Microsoft device code, on the interactive CLI path, which the
   server reaches never because `interactive=False` raises first).
3. `tests/test_standards_enforcement.py::test_nothing_but_the_cli_writes_to_stdout`
   asserts the **exemption list itself** has not grown — a per-file-ignore added
   to `pyproject.toml` is invisible in the linter's own output, so nothing else
   would notice.

### Never log secrets or private content

**SATISFIED — and this is the row an auditor should press hardest on, so it has
two independent mechanisms and a test that has been proven to fail.**

This connector reads a business owner's mailbox. A log file is a second copy of whatever
it records, in a place with different permissions, a different lifetime, and a
different backup policy than the mailbox — so logging one subject line silently
creates an unmanaged copy of confidential material.

**Mechanism 1 — the API only accepts identifiers.** `ProviderCall` and `ToolCall`
take `**fields` rendered as `key=value`, and every call site passes ids, counts,
and booleans. `create_draft` logs `body_len`, never `body`
(`server.py:568`, `providers/gmail.py:434`). Provider `authenticate` returns the
owner's address to the tool layer and does not log it
(`providers/gmail.py:196-199`).

**Mechanism 2 — a redacting patcher on every record.** `_redact`
(`logging_config.py:242`) is installed via `logger.configure(patcher=...)`, so it
runs on the record itself and **all three sinks receive the redacted form** —
none of them can be the one that leaks. Field names are matched
case-insensitively with separators normalised, so `API_KEY`, `api-key`, and
`apiKey` are one rule (`_normalize_name`, `logging_config.py:186`).

Covered names include the standard's full minimum set (`password`, `passphrase`,
`secret`, `token`, `api_key`, `authorization`, `cookie`, `session`,
`credential`, `private_key`) **plus this connector's own private content**
(`body`, `subject`, `snippet`, `address`, `recipient`, `sender`, `participant`,
`email`, `mail`, `content`, `message`, `text`, `profile`) — because a mailbox
connector that redacted only credentials would still write the mail itself into
the log.

Identifier and count suffixes are checked **first** (`ID_FIELD_SUFFIXES`,
`logging_config.py:175`), so `message_id` and `body_len` are logged while `body`
and `subject` are not — ids and counts being precisely what the standard says to
log instead of the value.

Sanitization is **bounded** per the standard: depth 4, 50 items per container,
200 characters per retained string (`logging_config.py:121-127`). Unknown objects
and exceptions get an allowlisted type summary — **never a fallback `repr`**
(`logging_config.py:228-230`), which is the side door a token or a mail body
would otherwise come back through.

Two further content bounds:

- A **rejected** taxonomy folder is truncated to 40 characters before logging
  (`taxonomy.py:97-110`). It is the one value on that path an attacker can
  influence — a hostile email talks the AI into proposing it — so an unbounded
  write would let mail content reach the log through the folder argument. The
  length is what makes the attempt diagnosable; the text is not.
- Graph error bodies are truncated to 300 characters before entering an
  exception message (`providers/graph.py:MAX_ERROR_DETAIL_CHARS`), and log lines
  carry the request **path only, never the query string** (`_path_of`) — because
  `$filter` carries `conversationId` and the owner's date selections.
- `diagnose=False` on both file sinks (`logging_config.py:314,330`): `diagnose`
  renders local variable *values* into tracebacks, and frames in this package
  hold mail bodies and OAuth tokens.

**THE TEST YOU ASKED FOR, AND ITS RED PROOF.**
`tests/test_logging_redaction.py::test_a_full_sweep_writes_no_mail_content_to_any_sink`
drives a complete mocked mailbox through the connector — `list_inbox`,
`get_thread`, `read_sent_sample`, `file_message`, `mark_important`,
`create_draft`, `get_voice_routing`, `get_daily_digest` — with every log sink
captured, then asserts that no line contains the mock's body, subject, snippet,
sent-body, address, or credential canaries.

- It asserts the sweep **produced log lines at all**, so redaction cannot be
  achieved by logging nothing.
- `test_the_sweep_did_log_something_diagnosable` asserts the five points are
  present and that ids *are* logged.
- **RED-verified 2026-08-17:** planting `call.fields["subj"] = thread.subject`
  in `server.get_thread` produced
  `FAILED ...test_a_full_sweep_writes_no_mail_content_to_any_sink`; reverting
  returned the suite to green. **This test has been observed failing on a real
  leak.**
- `test_a_failing_provider_call_logs_the_failure_without_the_content` covers the
  exception path specifically — an error message that interpolated a provider
  response carries whatever that response held.

### Crash visibility

**SATISFIED.** `install_crash_handler` (`logging_config.py:479`) logs an
unhandled exception at CRITICAL with its traceback before the process dies, and
is installed by `load_settings`. `KeyboardInterrupt` passes through untouched —
a Ctrl-C is an operator decision, and logging it at CRITICAL trains people to
ignore the level.

### Retries and outbound calls

**SATISFIED.** Bounded (3 attempts, `providers/retry.py:46`), each failure
logged with its attempt count (`before_sleep` → `call.retry`), re-raised on
exhaustion (`reraise=True`, `providers/retry.py:121`) — never a wrapper that
hides which provider error actually occurred. Explicit HTTP timeout on Graph
(`_TIMEOUT = httpx.Timeout(30.0)`). Response handling is bounded: page sizes are
clamped to 200 with a `guard()` line recording requested-vs-applied
(`server.py:MAX_INBOX_PAGE`, `MAX_SENT_SAMPLE`; `providers/*/MAX_PAGE_SIZE`).

### Enforcement

| Required check | Status |
| --- | --- |
| No empty catch/except blocks | ruff `BLE`, `try-except-pass` — selected, verified firing |
| Every except body logs or re-raises | **human-reviewed + verified by inspection**; all 7 `except` blocks in `src/` either log with `exc_info=True` or `raise ... from`. No mechanical rule exists for this in ruff. |
| No direct print outside the logging module | ruff `T20` + `test_nothing_but_the_cli_writes_to_stdout` |
| No floating promises | **N/A** — TypeScript rule |

---

## 3. `_DOCS/STANDARDS-core.md`

| Rule | Status |
| --- | --- |
| **Say What You Mean** | Every claim in this document names a file:line or a command and its observed output. Claims that are not RUNNING are labelled (see the status banner). |
| **Control Flow** (enum + table over conditionals) | See §1 "flat control flow". `preview = true` verified firing. |
| **Minimal Correct Change** | Tool behaviour is unchanged by this pass: the same 8 tools it started with (`survey_inbox` was added later, bringing the surface to nine), same wire schemas, `create_draft` still has no recipient parameter, no-send check still exit 0. Fixes landed at the owning boundary — env access moved to the keystone rather than being patched per caller; the drafts-bool guard lives on the model both `Settings` and `ConnectorConfig` delegate to (`config.py:504`), so the two cannot drift. |
| **Do not reinvent the wheel** | See §1. Four libraries adopted; two capabilities (retry, logging) did not exist at all before. |
| **Comments explain WHY** | ruff `ERA001` (commented-out code) and `PLR2044` (empty comment) selected and verified firing. Comments state reasons and constraints; `# Reason:`-style rationale is attached to every non-obvious choice. |
| **Verification** | §0 lists the exact commands and their observed output. |
| **Secrets** | No secret value in source, tests, fixtures, logs, or this document. Test canaries are named `CANARY_*` / `MOCK_TRANSPORT_SENTINEL` — deliberately *not* credential-shaped names — so flake8-bandit's `S105`/`S106`/`S107` stay ON for the whole suite rather than being suppressed for the one file whose job is proving credentials do not leak. |
| **Workspace hygiene / never `/tmp`** | No `/tmp` path in this package. Scratch used during this pass was `the repo's temp workspace`. |
| **No hidden dot-folders** | The only dot-entries are `.gitignore` and `.python-version` (tool-mandated names, per the repo's 2026-08-11 ruling). |

---

## 4. Explicit N/A list, with reasons

An auditor should be able to see what is *not* claimed, and why.

| Rule | Why N/A |
| --- | --- |
| `_githooks/` + `core.hooksPath` | Git hooks are repo-scoped, and this connector is a subdirectory of `rtech-consulting`, not its own repository. It cannot set `core.hooksPath` without hijacking the whole repo's hooks. **The checks themselves are not skipped** — they are wired into the test suite (`tests/test_standards_enforcement.py`), which runs on every `uv run pytest`. When this connector is extracted to its own repo, `scripts/check_code_size.py` is hook-ready as-is. |
| `.github/workflows/` CI | The parent repo has no CI (recorded as outstanding in its own `AGENTS.md` banner). Nothing to hook into. Every gate is a one-line local command (§0). |
| `vaultwarden-secrets` fetch | Contradicts the vendor-agnostic doctrine for a connector that runs on the buyer's machine. See §1. |
| `generate_folder_docs.py` / generated per-folder `README.md` | The README is hand-authored buyer-facing security documentation, not a generated artifact. See §1. |
| `config/` + `secrets/` committed layer directories | The connector's config is written at INSTALL time to an OS-standard per-user directory (`platformdirs`), not shipped in the source tree. There is no committed config layer to split, and a `secrets/` directory would contradict "nothing token-like is ever written to a file". |
| Coverage gate | Forbidden by the standard. Deliberately absent. |
| No floating promises | TypeScript rule. |
| `py.typed` marker | The standard says to add it only to packages you actually distribute. This is an application installed from source. |
| Every-except-logs custom lint rule | The standard names it as required, but no ruff rule expresses it and this connector has no CI to host a custom one. Verified by inspection instead, and recorded as such rather than claimed as mechanical. **This is the one required enforcement mechanism this package does not have.** |

---

## 5. What this pass changed, in one list

New files: `logging_config.py`, `providers/retry.py`, `providers/factory.py`,
`scripts/check_code_size.py`, `tests/test_logging_redaction.py`,
`tests/test_standards_enforcement.py`, this document.

Modified: `config.py` (rewritten as the keystone), `paths.py` (now a shim),
`auth.py`, `ledger.py`, `taxonomy.py`, `server.py`, `cli.py`,
`providers/{__init__,base,gmail,graph}.py`, `pyproject.toml`, and four test
files.

Dependencies added: `pydantic`, `pydantic-settings`, `loguru`, `tenacity`.

Gate movement: tests 223 → 290; ruff rule selectors 9 → 27; environment-reading
modules 3 → 1; `@dataclass` models 6 → 0; logging statements 0 → full five-point
coverage on 24 call sites.

**Unchanged, and verified unchanged:** the eight MCP tools this pass covered
and their wire schemas, `create_draft`'s absent recipient parameter, the scope
manifest, and `_graph/done-means/inbox-zero-no-send.sh` → **EXIT=0**. The
read-only `survey_inbox` and the mutating `record_correction` added afterwards
make the current surface ten tools and change none of the above.
