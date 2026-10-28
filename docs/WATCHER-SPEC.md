# Watcher Loop — Design Spec

**Status: PROPOSED. Nothing is built.** No module named here exists. No source
file was modified to produce this document. Every provider fact is either cited
to a brief under a research scratch folder, cited to
a source file in `connector/src/inbox_zero_connector/`, or explicitly marked
**UNVERIFIED**. Implementers: an UNVERIFIED claim is a live test you owe before
the behaviour it describes reaches a client mailbox.

**What this specifies:** the unattended polling loop that sits above
`watchstate.py`. It is the module `watchstate.py`'s own docstring declines to be
— *"The loop that calls a provider and acts on the results is another module's
job; this one only remembers where that loop got to."*

---

## 0. Settled inputs — do not re-derive

These are decided. Reopening any of them is scope creep, not diligence.

| Fact | Where it was settled |
| --- | --- |
| Trigger is **delta polling, not webhooks**. A small office behind NAT has no publicly reachable HTTPS endpoint, and Graph states twice it does not deliver without one. Dropped push notifications "can't be recovered." Gmail's `users.watch` needs Cloud Pub/Sub and a reachable endpoint too — polling is right on **both**. | `graph-watch-brief.md` §4a, §3; `provider-parity-audit.md:223-233`; restated `watchstate.py:24-49` |
| Graph has **no mailbox-wide delta**. `/me/messages/delta` is not documented and does not exist; coverage costs N per-folder streams and N independently-evicting tokens. Gmail has **one** mailbox-wide `historyId`. | `rules-interop-brief.md:168-204`; `provider-parity-audit.md:366` |
| `WatchCursor` already models both shapes: opaque `token`, `container_id: str \| None`. Gmail stores one row with `None`; Graph stores N. | `watchstate.py` `WatchCursor.container_id` |
| Graph reports a **move as `@removed` / `reason: deleted`**, byte-identical to a real delete. "Delta tracking operates at the collection level, not the message level." No correlation id, no ordering guarantee. DOCUMENTED impossible at the API. | `rules-interop-brief.md:250-293` |
| Output is a **LABEL, never a folder move.** Graph: read-union-write PATCH of `categories` with `If-Match`. Gmail: `messages.modify` `addLabelIds`, an append primitive. | `if-match-live-test.md`; `provider-parity-audit.md` §2.1 |
| The seen-ids guard bounds replay at **500 per container**; beyond that a replay looks new and IS reprocessed. It makes reprocessing unlikely, **not** the pipeline idempotent. | `watchstate.py` `SEEN_LIMIT` |

Two facts from the audits invert premises a reader may carry in. Both change what
the loop does, so they are stated here rather than left in a brief.

> **(a) On Gmail, scope is an over-collection problem, not a coverage problem.**
> `mailboxscope.py:10-15` states the problem as Exchange rules moving mail out of
> Inbox before an Inbox-only watcher sees it. That premise is **false on Gmail**: a
> filter's "skip the inbox" compiles to `action.removeLabelIds: ["INBOX"]`, which
> removes a tag rather than moving the message, and the message still generates a
> `messageAdded` record on a stream that is mailbox-wide by definition. An
> unfiltered `history.list` poller therefore sees **more** than the inbox —
> archived and filtered mail included. The loop must apply the scope check itself
> and must not assume the provider narrowed the stream for it.
> (`provider-parity-audit.md:247-269`. Marked there **UNVERIFIED by direct quote,
> HIGH CONFIDENCE by construction**; the over-collection posture is correct either
> way, because the conservative direction costs nothing.)

> **(b) Graph does not REJECT a move via PATCH — it silently IGNORES it.**
> `PATCH {"parentFolderId": <Archive>}` returns `200 OK` and the message **stays
> in the Inbox** (LIVE-TESTED, `categories-live-test.md:138-149`). A bogus folder
> id also returns `200` with no validation (`:155-157`), and the ignore is
> **per-field** — a request carrying both `categories` and `parentFolderId`
> applies the category and drops the move (`:159-165`). Contrast an unknown
> property, which *does* fail `400 RequestBodyRead` (`:166-171`).
>
> Two consequences. **For the implementer:** a `200 OK` from a category PATCH is
> not evidence that every field in the body was honoured. **For buyer copy:** we
> must never write that Graph "rejects" a move — it does not. `POST
> /me/messages/{id}/move` remains available under our scopes, so the no-move
> guarantee is a **code-layer guarantee, not a scope-layer one**
> (`categories-live-test.md:252-263`). That is exactly what §7 is for.

---

## 1. The loop's shape — an enum and a table

`_DOCS/STANDARDS-python.md ## LAW: flat control flow` requires the closed set to be
an enum and the transitions to be a table a reader can check for completeness. The
loop below has **one** `match` on `RoundState` and no nested conditionals; every
unhappy path is a named state, not an `except` block buried three frames down.

```python
class RoundState(StrEnum):
    """Every state one polling round can be in. Closed set, checked exhaustively."""

    COLD_START      = "cold_start"       # no cursor for this stream, ever
    DELTA           = "delta"            # normal: replay the stored cursor
    PAGING          = "paging"           # mid-round, provider handed a next-page link
    RESYNC_REQUIRED = "resync_required"  # provider rejected the token
    THROTTLED       = "throttled"        # provider said slow down
    COMMIT          = "commit"           # final link received, cursor may be stored
    ABORTED         = "aborted"          # round ends with NO cursor written
```

`ROUND_TABLE` is data, in the same shape as `classify.RULES` and `config.VOICE_RULES`
— an ordered tuple of rows, not a chain of ifs. Adding a state is one row.

| # | State | Entry condition (what the provider just did) | Action taken | Next state | Cursor written? |
| --- | --- | --- | --- | --- | --- |
| 1 | `COLD_START` | `state.cursor_for(container_id)` returned `None` | Full sync with **no token**. Graph: `GET .../messages/delta` bare. Gmail: `messages.list` + per-message `messages.get`. | `PAGING` or `COMMIT` | no |
| 2 | `DELTA` | A cursor exists | Replay `cursor.token` verbatim. Graph: GET the stored `@odata.deltaLink` URL whole. Gmail: `history.list?startHistoryId=<token>` | `PAGING`, `COMMIT`, `RESYNC_REQUIRED`, or `THROTTLED` | no |
| 3 | `PAGING` | Provider returned a **continuation** link (Graph `@odata.nextLink`, Gmail `nextPageToken`) | Process this page's items, then fetch the next page | `PAGING`, `COMMIT`, `RESYNC_REQUIRED`, `THROTTLED`, or `ABORTED` | **NO — see §2.4, the load-bearing prohibition** |
| 4 | `RESYNC_REQUIRED` | Graph `410 Gone` (with a `Location` header carrying an empty `$deltatoken`) or `syncStateNotFound`; **Gmail `404`** on an out-of-range `startHistoryId` | `state.resync_required(container_id)` → drops cursor, bumps generation, **keeps seen-ids** | `COLD_START` (next round, not this one) | no — the cursor is *deleted* |
| 5 | `THROTTLED` | Graph `429` + `Retry-After`; **Gmail `429` OR `403 rateLimitExceeded` / `403 userRateLimitExceeded`** | Sleep `Retry-After`, else truncated exponential backoff. Bounded by `retry.MAX_ATTEMPTS` = 3 | resume the state it came from, or `ABORTED` on exhaustion | no |
| 6 | `COMMIT` | Provider returned a **final** link (Graph `@odata.deltaLink`, Gmail a page with no `nextPageToken`) | `state.record_cursor(container_id, token)` then `state.save(provider)` | round ends | **YES — the only state that writes one** |
| 7 | `ABORTED` | Unhandled provider error, retry exhaustion, page-budget exhaustion, or a ledger write failure | Log at ERROR with the container and the provider code. Leave the cursor exactly as it was. | round ends | **no — and this is what makes abort safe** |

**Completeness check a reader can run.** Seven states; every non-terminal row names
its successors; the two terminal rows are `COMMIT` and `ABORTED`; exactly one row
writes a cursor. A state that could write a cursor from anywhere but row 6 is a bug
in the implementation, not a gap in this table.

**Classification uncertainty is deliberately NOT a state.** It is not a loop
condition, because `classify.coerce` already handles it inside the decision layer:
anything below `MIN_CONFIDENCE` (0.70) or outside the taxonomy becomes `Review` with
the original suggestion preserved in `reason` (`classify.py` `coerce`). The loop
receives a legal `Classification` in every case and never branches on confidence. A
`Review` label is a normal successful round, not a degraded one.

**Write failure is also not a state.** A failed `tag_message` is a per-message
outcome, receipted with `Status.FAILED` (§5), and the round continues to the next
message. A round does not abort because one message could not be labelled — that
would let one contended message block every other message behind it.

---

## 2. One full round, step by step

### 2.1 Common preamble (both providers)

1. `state = WatchState.load(provider)` — a missing or unparseable file is a first run
   and returns an empty state, not an error (`watchstate.py` `WatchState.load`).
2. `containers = provider.list_containers()` — needed to translate provider container
   ids into the display names and `well_known` tags `MailboxScope` consumes. Cache it
   for the round; do not re-fetch per message. Cost: Gmail `labels.list` 1 QU; Graph
   one folder walk. **Graph: filter out virtual search folders**
   (`#microsoft.graph.mailSearchFolder`) or they double-count
   (`rules-interop-brief.md:107-112`).
3. Determine the streams to poll:
   - **Gmail:** exactly one, `container_id=None`.
   - **Graph:** one per in-scope mail folder. A folder `scope.covers()` rejects is
     **not polled at all** (§4).

### 2.2 Microsoft Graph — one stream, one round

| Step | What happens |
| --- | --- |
| 1 | Read the cursor. `cursor = state.cursor_for(folder_id)`. `None` → `COLD_START`. |
| 2 | **Fetch.** `COLD_START`: `GET /me/mailFolders/{id}/messages/delta?$select=id,subject,from,receivedDateTime,isRead,parentFolderId`. `DELTA`: GET the stored `@odata.deltaLink` **URL whole, unmodified**. Do NOT re-append `$select` — the token already encodes the original query parameters, and re-supplying them changes the field set of the round with no error anywhere (`graph-watch-brief.md` §4c; `watchstate.py` `WatchCursor.token`). |
| 3 | **Skip removals.** An item carrying `@removed` is discarded without classification. A move and a delete are byte-identical here, with no correlation id and no ordering guarantee (`rules-interop-brief.md:250-293`), and this loop never acts on either — it only ever adds a label to a message that is present. |
| 4 | **Scope gate.** For each surviving item, `scope.covers(folder_name, well_known=tag)` on its parent folder. False → **discard now, before any content read** (§4). |
| 5 | **Replay gate.** `state.seen_message(folder_id, message.id)` → True means skip. Replays are documented and expected, the same entity can appear more than once in one round, and ordering is not guaranteed (`rules-interop-brief.md:244-248`). |
| 6 | **Decide.** `classify(message, taxonomy, classifier)`. Header projection only; `Message` carries no body (`base.py` `Message`). |
| 7 | **Write.** `provider.tag_message(message.id, classification.folder)` — the read-union-write PATCH of §3.1. |
| 8 | **Receipt.** `ledger.append(provider="microsoft", action=ACTION_FILE, message_id=..., detail=classification.folder)` on success; `status=Status.FAILED, error_code="412"` on refusal. |
| 9 | **Mark seen.** `state.mark_seen(folder_id, message.id)` — **after** the write attempt and its receipt, never before. Marking first would let a crash between mark and write suppress the retry on the next round. |
| 10 | **Page or commit.** `@odata.nextLink` → step 2 with that URL, state stays `PAGING`, **no cursor write**. `@odata.deltaLink` → `state.record_cursor(folder_id, delta_link)`, `state.save("microsoft")`. |

### 2.3 Gmail — one stream, one round

| Step | What happens |
| --- | --- |
| 1 | `cursor = state.cursor_for(None)`. `None` → `COLD_START`. |
| 2 | **Fetch.** `COLD_START`: `messages.list` (5 QU) bounded to the scope, then `messages.get` per id (**20 QU each — the expensive half**). `DELTA`: `history.list?startHistoryId=<token>&historyTypes=messageAdded` (2 QU). |
| 3 | **Filter record types.** Consume `messageAdded` only. `messageDeleted`, `labelAdded`, and `labelRemoved` are read-side noise for a labelling watcher and are dropped before scope. |
| 4 | **Scope gate — `covers_all`, not `covers`.** A Gmail message carries many labels at once, so the question is whether the watcher may read a message holding **all** of them. Any-exclusion-wins (`mailboxscope.py` `covers_all`). False → discard now, before any content read (§4). This gate does more work on Gmail than on Graph: the stream is mailbox-wide and includes archived and filtered mail the owner never scoped in (§0a). |
| 5 | **Replay gate.** `state.seen_message(None, message.id)`. Gmail message ids are **stable** across `modify` (`provider-parity-audit.md:370`), so the id is a sound key. |
| 6 | **Decide.** Same `classify` call. Gmail's `CATEGORY_PROMOTIONS` / `_UPDATES` / `_FORUMS` labels are already consumed by the rule table (`classify.py:133-154`), so promotional mail is settled deterministically and never reaches the model. |
| 7 | **Write.** `messages.modify` `{"addLabelIds": ["<label_id>"]}` with `removeLabelIds` **omitted**. 5 QU. Append primitive; no read, no etag, no race. |
| 8 | **Receipt.** `ledger.append(provider="google", ...)`, same shape. |
| 9 | **Mark seen.** `state.mark_seen(None, message.id)`, after the write. |
| 10 | **Page or commit.** `nextPageToken` present → next page, **no cursor write**. Absent → `state.record_cursor(None, response.historyId)`, `state.save("google")`. |

### 2.4 THE CURSOR RULE — stated once, explicitly, because it is the failure that is silent

> **The cursor is stored ONLY when the provider returned a FINAL delta link. Never on
> a paging link.**
>
> Graph: `@odata.deltaLink`, never `@odata.nextLink`. Gmail: a page with no
> `nextPageToken`, never one that has one. The two never appear on the same response
> (`graph-watch-brief.md` §4c).
>
> **Why this is the one to get right.** Storing a cursor mid-page marks a partial read
> as a completed one. The next round resumes from a position that claims everything
> before it was processed — and every change on the pages that were never fetched is
> gone, permanently, with no error, no exception, and no log line. The mailbox looks
> healthy. The watcher looks healthy. The mail is simply missing, and nothing in the
> system can tell you which. `watchstate.record_cursor`'s own docstring states this
> constraint; this spec restates it because the docstring is on the callee and the
> mistake is made by the caller.
>
> Corollary: a round that ends in `ABORTED` writes **nothing**. Re-reading a page is
> free (`seen_message` absorbs the replay); skipping one is not.

---

## 3. Idempotency at the action boundary

`SEEN_LIMIT` bounds replay at 500 ids per container and is honest that beyond that
window a replay "is treated as new and IS REPROCESSED" (`watchstate.py`). The guard is
a cost optimisation, not a correctness mechanism. Correctness has to live at the
action itself.

### 3.1 Is a repeated `tag_message` harmless?

**Gmail: YES, structurally — but the no-op claim itself is UNVERIFIED.**
`addLabelIds` is an append primitive: it "cannot destroy an existing label"
(DOC-CONFIRMED, `provider-parity-audit.md:158-163`, citing the `users.messages.modify`
reference). So a repeat cannot *clobber*. Whether re-adding an already-present label
is a true **no-op** is not stated by Google and is not asserted in any brief — it is
implied by the append semantics and nothing more. Treat it as **UNVERIFIED**; the
consequence of being wrong is benign (an identical label set), which is why this does
not block. A repeated call costs 5 QU.

**Graph: YES, but only because the read-union-write makes it so.** `categories` is a
**collection-replace, VERIFIED-LIVE**: PATCH `["B"]` over an existing `["A"]` returns
`200 OK` and **A is gone** — silent data loss with no error signal
(`categories-live-test.md:62-82`, EXP 3). PATCH `["A","B"]` persists both (`:83-93`,
EXP 4). There is no append primitive on Graph. So the write is:

```
GET /me/messages/{id}?$select=id,categories    ->  current set + @odata.etag
union our label into that set
if union == current set:  RETURN, write nothing        # the no-op case
PATCH /me/messages/{id}  If-Match: <etag>  {"categories": [union]}
```

The union step is what makes a repeat a no-op: the second call computes the same set,
finds it already present, and returns without issuing the PATCH at all. **Skipping
that write is required, not an optimisation** — the etag changed on every observed
write (`categories-live-test.md:206-208`), so a content-identical PATCH still bumps
the version and invalidates every other client's concurrent read for nothing.

**The rule the live test leaves behind:** never PATCH a hardcoded list. The set
written must derive from a value just read (`categories-live-test.md:200-202`).

### 3.1a `If-Match` — now VERIFIED-LIVE, and the details differ from the earlier design

`category-write-concurrency.md` marked `If-Match`-on-a-message-PATCH as UNVERIFIED and
demanded a live test. **That test has run** (`if-match-live-test.md`, status RUNNING)
and it corrects three things:

| Input | Status | `error.code` | Write landed? |
| --- | --- | --- | --- |
| current etag | `200` | — | yes |
| **stale** well-formed etag | **`412`** | **`ErrorIrresolvableConflict`** | **no** |
| garbage (`W/"nonsense"`) | `400` | `ErrorInvalidChangeKey` | no |
| `If-Match: *` | `400` | `ErrorInvalidChangeKey` | no |

1. **Branch on HTTP status 412, NEVER on `error.code`.** The predecessor spec guessed
   `preconditionFailed`; that guess was **wrong** — it is `ErrorIrresolvableConflict`
   (`if-match-live-test.md:119-124`).
2. **The `W/` prefix is OPTIONAL.** Behaviour is identical with or without it, tested
   on both a matching and a stale pair (`:146-201`). The planner's `W/`-stripping
   requirement does not apply and needs no fallback.
3. **There is no `ETag` HTTP response header.** Every response reported `ETag: None`.
   The etag is body-only via `@odata.etag`, emitted regardless of `$select`
   (`:56-63`). `response.headers["ETag"]` returns nothing — read the body.

**A 412 is a clean refusal.** EXP 4 confirmed that after a 412 the categories **and**
the etag are both unchanged: the rejected request did not even bump the version
(`:126-144`). That is what makes re-GET → re-union → re-PATCH safe and convergent
rather than a race against a moving target.

> **The one genuinely open item, and it is unverifiable by testing:** `If-Match` is
> **not documented** by Microsoft in the `message: update` request-headers table
> (`if-match-live-test.md:277-284`). The behaviour is live-observed, not
> vendor-committed, and can change without a deprecation notice. Treat an unexpected
> `200` on a stale etag as a signal to re-verify, not as a lucky pass.

### 3.2 What would NOT be idempotent if the loop grew a drafting step

Stated now, while it is cheap, because the guard's own docstring names this exact
scenario: *"a second draft is a duplicate in the owner's Drafts folder and a second
ledger row"* (`watchstate.py` `SEEN_LIMIT`).

| Hypothetical step | Idempotent? | Why |
| --- | --- | --- |
| `tag_message` | Yes | Union (Graph) / append (Gmail). §3.1 |
| `set_important` | Yes | Sets a boolean to a value. Setting True twice is one state. |
| **`create_draft`** | **NO** | Creates a **new** draft each call. Two rounds → two drafts in the owner's Drafts folder, two `ACTION_DRAFT` ledger rows, one real intent. 10 QU on Gmail. The 500-id window makes this unlikely; it does not make it safe. |
| `file_message` | No, and worse than not-idempotent | Contractually destructive — moves the message and removes it from the inbox (`base.py` `file_message`). On Graph the move is copy-then-delete, so the message gets a **new id** (`provider-parity-audit.md:370`) — which also breaks the seen-ids key. This loop never calls it. §7. |

**The rule for any future mutating step: it carries its own idempotency key at its own
boundary, or it does not go in the loop.** For drafting that means a provider-side
existence check before creating (does a draft already exist on this thread from this
watcher) or a durable per-message action record the loop consults — not a wider
`SEEN_LIMIT`. Raising the bound trades a fixed, knowable cost for an unbounded one and
still does not close the window.

---

## 4. Scope enforcement

**Where `covers` / `covers_all` is called, and it is exactly one place per provider.**

| Provider | Call | Placed |
| --- | --- | --- |
| Graph | `scope.covers(folder.name, well_known=folder.well_known)` | **Twice, deliberately.** (a) When enumerating streams — an out-of-scope folder is **never polled**, so its messages never enter the loop. (b) Per item, on its parent folder, because a message can arrive in a stream after being moved. |
| Gmail | `scope.covers_all(names, well_known=tags)` over **every** label the message carries | Once, per message, immediately after the history record is decoded and **before anything else touches the message**. There is no stream-level gate available: `history.list` is mailbox-wide and `labelId` filters *records*, not the stream (`provider-parity-audit.md` §2.2). |

**An excluded message is discarded BEFORE classification, not after.**

This ordering is the whole point and it is not a performance preference. Classification
means **reading the message** — `classify` consumes subject, sender, and snippet, all of
which are message content. A pipeline that classifies first and filters second has
already read the excluded mail; the discard afterwards deletes a variable, not the
read. For a mailbox that may hold confidential client material, the read is the event the
client cares about, and it cannot be taken back.

Therefore the per-message order is fixed: **decode → scope → replay → classify → write
→ receipt → mark seen.** Scope precedes classify, always. An implementation that
computes a `Classification` and then checks scope is wrong even if its observable
labelling behaviour is identical.

Precise behaviour for an excluded message:

- It is **not** classified, **not** labelled, **not** ledgered, and **not** marked seen.
- It is **not** an error and does not abort the round. Being out of scope is the normal
  state of most mail in a scoped deployment.
- It logs at DEBUG with the **container name only** — never subject, sender, or snippet.
  A log line naming what we refused to read is a smaller version of reading it.
- Not marking it seen is deliberate: the seen window exists to suppress duplicate
  *actions*, and no action occurred. Spending a slot on a message the loop will re-reject
  identically evicts an id that actually needs protecting.

`covers_all`'s any-exclusion-wins rule and its empty-sequence-is-not-covered rule
(`mailboxscope.py` `covers_all`) are inherited whole. The loop does not re-derive scope
from `MailboxScope`'s raw fields anywhere — "a second implementation of this decision is
a second chance to read an excluded folder" (`mailboxscope.py` `covers`).

> **Known gap, inherited and not solved here: nested-label exclusion on Gmail.**
> Excluding `Clients` on Graph excludes the whole subtree, because Graph folders
> genuinely nest. On Gmail it excludes **one flat string** and silently fails to
> exclude `Clients/Smith` — the Gmail `Label` resource carries no parent field at all,
> so the `/` is a UI display convention and **UNVERIFIED as an API contract**
> (`provider-parity-audit.md:72`, `:178-186`). This is a real exposure with the
> signature the scope rules exist to prevent: no error, no log line. It belongs to
> `mailboxscope.py`, not to the loop, and the loop must not paper over it with a
> prefix match of its own — a second implementation of the scope decision is exactly
> what `covers` forbids. **Flagged for the owning lane.**

---

## 5. Failure handling

`_DOCS/STANDARDS-python.md ## LAW: fail loudly`. Nothing here returns a sentinel and
nothing swallows an exception.

| Failure | Round | Message | Retry | Ledger | Log |
| --- | --- | --- | --- | --- | --- |
| **Provider throttle** — Graph `429` + `Retry-After`; Gmail `429` **or `403 rateLimitExceeded` / `403 userRateLimitExceeded`** | Continues after the wait | resumed | **YES.** Honour `Retry-After` exactly; absent it, truncated exponential backoff `min((2^n)+random_ms, max)` starting ≥1s. Bounded by `MAX_ATTEMPTS`=3. Immediate retry is strictly worse — Graph keeps logging usage while throttling | none (no mutation attempted) | WARNING with status and wait |
| **Retry exhaustion** on a fetch | `ABORTED`, **no cursor written** | n/a | no | none | ERROR with container and attempt count |
| **`412` on the categories PATCH** (`error.code: ErrorIrresolvableConflict`) | Continues | this one retried | **YES, up to 3 total attempts — each attempt must re-GET, re-union, re-PATCH.** Replaying the identical body reintroduces the exact clobber `If-Match` prevents. **Branch on HTTP status 412, never on `error.code`.** Safe to converge: a 412 leaves categories and etag both unchanged (`if-match-live-test.md:126-144`) | on the 3rd failure: `Status.FAILED`, `error_code="412"` | WARNING per attempt, ERROR on exhaustion |
| **`400 ErrorInvalidChangeKey`** | Continues | **skipped** | **NEVER.** A malformed or garbage etag — our own bug. LIVE-CONFIRMED for both garbage values and `If-Match: *`. A retry loop keyed on "not 200" spins 3× on our own defect (`if-match-live-test.md:304-308`) | `Status.FAILED`, `error_code="ErrorInvalidChangeKey"` | ERROR, named as a connector defect |
| **`403 ErrorAccessDenied`** (Graph, generic) | `ABORTED` | n/a | **NEVER.** A scope refusal is a configuration fact. Graph returns a **generic** `ErrorAccessDenied` naming no missing scope — **do not parse the permission out of the body** (`categories-live-test.md:126-128`) | `Status.FAILED` if a mutation was in flight | ERROR |
| **`401` / other `403` without a rate-limit reason** | `ABORTED` | n/a | **NEVER.** `retry.py` excludes these on purpose, and it is a security property: a connector hammering a refusal looks exactly like one trying to get around it | as above | ERROR |
| **Classifier error** (injected classifier raises) | Continues | **skipped** | **NEVER** at the loop level. A model call that raised gets no second guess from here | none — no mutation was attempted, and `ledger.py` `Status` has exactly two states because "a decision never attempted against the mailbox is not a mutation" | ERROR with message id and exception type, **never the message content** |
| **Ledger write failure** (`OSError`) | **`ABORTED`, immediately** | n/a | no | n/a — the ledger is what failed | ERROR |
| **`410 Gone` / `syncStateNotFound` (Graph) / `404` (Gmail)** | **Not a failure.** `RESYNC_REQUIRED`, row 4 | n/a | full sync next round | none | INFO — `resync_required` already logs it as expected operation |

**Never retried, named explicitly:** `400 ErrorInvalidChangeKey`, `401`, `403
ErrorAccessDenied`, any `403` without a rate-limit reason, a raising classifier, and
**any mutation whose outcome is ambiguous** (a timeout on `tag_message` may have
applied the label — a blind second attempt is how one action becomes two ledger rows
and breaks the "action count == ledger row count" invariant the kit is audited on,
`retry.py` module docstring).

**One live-verified subtlety the retry policy must respect:** Gmail signals rate
limiting with **`403` as well as `429`**. `retry.py`'s `RETRYABLE_STATUS` is
`{408, 429, 500, 502, 503, 504}` and handles 403 as a body-conditional case via
`RATE_LIMIT_REASONS`. That is the correct shape — but the audit explicitly did **not**
verify it (`provider-parity-audit.md:355-357`, `:659-661`), and a 403-only-fatal policy
would fail a perfectly healthy Gmail poller. **Confirm before shipping** (§9).

**The forbidden fallback, stated so it cannot be written by accident:**

```python
# WRONG -- reintroduces the exact data loss If-Match exists to prevent
except PreconditionFailed:
    patch(categories=union, headers={})   # no If-Match, force it through
```

Dropping `If-Match` to force a write past a 412 is never correct
(`if-match-live-test.md:317-320`). Three lost races means a human is actively editing
that message; deferring to them is the right outcome for any shared business mailbox.

**Why a ledger failure aborts the whole round when nothing else does.** The ledger is
the kit's evidence. A mutation that reached the mailbox with no durable receipt breaks
the invariant the product is sold on, and continuing would produce more unreceipted
mutations. `ledger.append` already re-raises `OSError` rather than swallowing it; the
loop must not catch it.

> **Resolved since the briefs were written.** Both `category-write-concurrency.md` and
> `if-match-live-test.md:341-345` carry a BLOCKER stating the connector has no failure
> ledger — `LedgerEntry` frozen, `extra="forbid"`, no status field. **That change has
> landed.** `ledger.py` now has `Status.APPLIED` / `Status.FAILED`, an `error_code`
> field, and `SCHEMA_VERSION` 3. The fallback those briefs describe (raise without a
> row) is no longer needed — write the FAILED row.

---

## 6. Polling interval

**Default: 60 seconds per mailbox.**

**Latency.** Documented Graph push latency for `message` is under 1 minute average, 3
minutes maximum (`graph-watch-brief.md` §Honest caveats). A 60-second poll is therefore
comparable to push in practice. A 5-minute poll is not, and the brief says so directly.
If a client states a genuine sub-minute requirement, that is the argument for a push
tier — make them state it rather than assuming it.

**Cost, per mailbox per day, at 60s (1,440 rounds/day):**

| Provider | Poll cost | Daily |
| --- | --- | --- |
| Gmail | `history.list` = **2 QU** | 2,880 QU/day, ~2 QU/min against a **6,000 QU/min per-user** budget — about **0.03%** |
| Graph | 1 request per in-scope folder | 1,440 requests/folder/day = ~10 per 10 minutes, against a per-mailbox budget in the thousands |

Neither poll is a meaningful cost. **The fan-out is where the spend is**, and it scales
with mail volume rather than with the interval: Gmail `messages.get` is 20 QU (10× the
poll) and `messages.modify` is 5 QU per labelled message
(`provider-parity-audit.md:334-343`). Shortening the interval buys latency almost for
free; the per-message work is what a busy mailbox actually costs.

> Two figures carry **UNVERIFIED** markers and must not be quoted to a client.
> (a) Graph's per-mailbox limit — the commonly cited 10,000 requests per 10-minute
> window per app+mailbox appears only in Q&A threads, not the reference table, whose
> Outlook section failed to render on two fetches (`graph-watch-brief.md` §6).
> (b) Gmail's 250 QU/user/second is a 2015 release note, **UNVERIFIED as current** —
> budget against the 6,000 QU/min figure instead, and note a 2026-05-01 release note
> says `messages.get` costs changed, so **re-check unit costs before hardcoding them**
> (`provider-parity-audit.md:337-339`).

**Microsoft's anti-polling guidance does not apply here, and the distinction is
precise.** Graph warns against "continuously polling a resource" and "regularly scanning
resource collections" — and names **change tracking** as the remedy, co-equal with
webhooks. The anti-pattern is `GET /me/messages` on a loop. Delta polling *is* the
recommended pattern (`graph-watch-brief.md` §6). Relatedly, do not "optimise" a cold
start by raising `$top` on a full listing: large `$top` with full payloads is a
documented **504 gateway timeout** trigger (`rules-interop-brief.md:153-157`).

**Staggering — 40 mailboxes for an MSP.**

Do not run 40 mailboxes on the same tick. A synchronised fleet turns a 60-second
average into a 40-request burst every 60 seconds, which is the shape that trips
per-app throttling while the average sits near zero.

- **Deterministic phase offset per mailbox**, not random: `offset = hash(mailbox_id) %
  interval_seconds`. At 40 mailboxes on 60s that is a poll roughly every 1.5 seconds,
  steady state. Deterministic so a restart resumes the same phase rather than
  re-clustering every mailbox onto the restart instant — which is exactly when a fleet
  is most likely to synchronise.
- **Per-mailbox jitter is not a substitute.** Random jitter reduces collisions on
  average and still allows a bad draw; a fixed offset cannot collide with itself.
- **A throttled mailbox backs off alone.** Throttle handling is per-stream state. One
  contended mailbox must not pause the other 39.
- **Cap concurrency below the fleet size.** 40 mailboxes do not need 40 in-flight
  rounds; a small worker pool over the offset schedule bounds peak concurrent requests
  independently of how many mailboxes are enrolled.
- Graph dispatches at most **4 concurrent requests** from a JSON batch to the Outlook
  service, and a 429 *inside* a batch is not auto-retried by the SDKs (the batch itself
  returns 200) — if batching is ever introduced, that retry is the caller's. Gmail
  batches cap at 50 requests.
- **At MSP scale the binding Gmail constraint is per-PROJECT**, not per-user: 1.2M
  QU/min across all mailboxes. At 60s polling the headroom is enormous, but if the kit
  ever runs many tenants under one Cloud project, `users.watch` (100 QU once/day) is
  the scale answer (`provider-parity-audit.md:346-349`). Not a v1 concern; noted so it
  is not rediscovered under load.

---

## 7. What the loop must NEVER do

Absolute. No task, deadline, or client request makes an item here conditional.

1. **NEVER move a message.** No `file_message`, no `POST /me/messages/{id}/move`, no
   `removeLabelIds: ["INBOX"]`. `base.py` `file_message` is contractually destructive —
   it moves and removes from the inbox. The watcher's output is a label; the message
   stays exactly where the owner left it.

   **This is a CODE-layer guarantee, not a scope-layer one.** Our scopes permit
   `POST /me/messages/{id}/move`, and Graph does not stop us
   (`categories-live-test.md:259-263`). Nothing but this rule and the tests that
   enforce it stands between the connector and a move. Buyer copy must say so
   accurately: Graph **silently ignores** a `parentFolderId` in a PATCH — it does not
   reject one (§0b).
2. **NEVER remove a label or a category.** The write is additive on both providers:
   `addLabelIds` with `removeLabelIds` **omitted**, and a `categories` union that only
   ever grows. A union that could shrink is a clobber, and on Graph the clobber is
   `200 OK` with no error (§3.1).
3. **NEVER send.** Nothing in this package reaches a send endpoint and the loop does not
   change that. The connector's Mail.ReadWrite-and-not-Mail.Send posture survives the
   watch tier intact (`graph-watch-brief.md` §1).
4. **NEVER delete.** Not messages, not folders, not labels, not categories.
5. **NEVER write a cursor it did not receive.** No constructing, parsing, normalising,
   incrementing, or comparing a token for ordering. It is opaque, replayed byte for
   byte. A "helpfully" normalised deltaLink silently changes the field set of the next
   round (`watchstate.py` `WatchCursor.token`).
6. **NEVER write a cursor from a paging link.** §2.4. The single silent-data-loss bug
   in this design.
7. **NEVER classify a message outside scope.** §4. Not classify-then-discard — the
   classification is the read.
8. **NEVER create a draft.** Not in this loop, not as a convenience. `create_draft` is
   not idempotent (§3.2) and adding it changes what the watcher is.
9. **NEVER log message content.** Not subject, sender, snippet, or body — in any log
   line, at any level, including error paths. Provider free-text error messages can
   quote a subject, which is why `error_code` on a ledger row is a **code, never a
   message** (`ledger.py` `error_code`).
10. **NEVER drop `If-Match` to force a write past a 412.** §5.
11. **NEVER PATCH a hardcoded category list.** The set written must derive from a value
    just read. `categories` is a collection-replace and the clobber is silent
    (`categories-live-test.md:200-202`).
12. **NEVER treat a resync as an error.** Token expiry is expected operation on both
    providers; a watcher that raises on it raises on a healthy quiet mailbox
    (`watchstate.py` `resync_required`).
13. **NEVER clear the seen-ids window on resync.** A resync re-reads everything the
    stream covers — it is precisely when the replay guard earns its keep.
14. **NEVER parse a missing permission out of a Graph 403 body.** It returns a generic
    `ErrorAccessDenied` naming no scope (`categories-live-test.md:126-128`).

---

## 8. Module boundary

### 8.1 What is new, and what is emphatically not

**New: `watcher.py`** — the loop, and nothing else. It orchestrates modules that
already exist and owns no decision any of them owns.

**Not new, and not modified by this work:**

| Module | Already owns | The loop's relationship |
| --- | --- | --- |
| `watchstate.py` | Cursors, generations, seen-ids, atomic persistence | Calls it. Adds no state of its own. |
| `classify.py` | The filing decision, the rule table, the coercion invariant | Calls `classify()`. Never inspects confidence, never re-implements coercion. |
| `mailboxscope.py` | `covers` / `covers_all`, any-exclusion-wins | Calls it. Never reads `MailboxScope`'s raw fields. |
| `ledger.py` | Receipts, `Status`, `error_code` | Calls `append()`. |
| `providers/retry.py` | Transport-level retry for reads | Used for fetches. The 412 loop does **not** go here — §8.3. |
| `providers/base.py` | The `MailProvider` contract | Consumes it. **`tag_message` is a separate lane's work** and is assumed to exist with the signature in §8.4. |

**One requirement outside this module, flagged not specified:** `MailProvider` has no
`tag_message` today — `provider-parity-audit.md:368` states it plainly: *"NO — the
method does not exist."* `ONBOARDING-SPEC.md:1559-1577` offers the toggle and no code
implements it on either provider. This spec assumes the signature in §8.4. Three lanes
are concurrently editing `providers/`, `ledger.py`, and `base.py`; the exact shape is
theirs to land, and if it differs, §8.4 is what changes — not §§1-7.

**A second prerequisite the Outlook lane owns:** `_patch` at `outlook.py:628` takes no
`headers` argument, so `If-Match` cannot be sent at all until it grows one. The live
test bypassed it with raw `httpx` (`if-match-live-test.md:341-345`).

### 8.2 Staying under the size limits

`_DOCS/STANDARDS-python.md` caps a module at 500 code lines and a function at 50.
The table-driven shape is what makes that hold rather than a promise to be brief:

- **The state machine is data.** `RoundState` plus `ROUND_TABLE` is ~30 lines of rows.
  A branch tree over the same seven states with their transitions is several hundred,
  and it grows superlinearly with each new state.
- **Per-provider fetch mechanics live behind `MailProvider`**, in adapters this module
  does not touch. `watcher.py` has no `if provider == "google"` anywhere — the
  divergence is absorbed by `container_id: str | None` (one stream vs N) and by the
  adapters' own `tag_message`.
- **The decision, the scope rule, the receipt, and the persistence are all delegated**
  to modules that already exist. `watcher.py` contains no classification logic, no
  scope logic, no file I/O, and no HTTP.

Projected: **~280-340 code lines**, largest function `_process_message` at ~35.

If it exceeds 500, the split is `watcher.py` (states, table, round driver) and
`watcher_round.py` (per-message pipeline). Split on that seam, not by line count.

### 8.3 Where the 412 loop lives — the call site, not `retry.py`

`retry.py` explicitly excludes mutations by design, its `RETRYABLE_STATUS` frozenset is
transport-level, and 412 is a **semantic** outcome — the server did exactly what was
asked and the answer is "your view is stale." Adding 412 there would make every PATCH
in the connector retry on 412 whether or not it recomputes its body, which is the
clobber (`category-write-concurrency.md` §"Where the retry belongs"). The re-GET →
re-union → re-PATCH loop belongs in the **Outlook adapter's `tag_message`**, importing
`MAX_ATTEMPTS` from `retry.py` so the kit keeps one number.

### 8.4 Function signatures

```python
# --- watcher.py -----------------------------------------------------------

class RoundState(StrEnum):
    """Every state one polling round can be in. See the table in §1."""
    COLD_START = "cold_start"
    DELTA = "delta"
    PAGING = "paging"
    RESYNC_REQUIRED = "resync_required"
    THROTTLED = "throttled"
    COMMIT = "commit"
    ABORTED = "aborted"


class RoundOutcome(BaseModel):
    """What one stream's round did. Frozen; a completed round is a fact.

    Returned rather than logged-and-discarded so the caller can decide whether
    the fleet is healthy without parsing log lines.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    container_id: str | None
    final_state: RoundState
    fetched: int          # items the provider returned
    out_of_scope: int     # discarded at the scope gate, never classified
    replayed: int         # suppressed by the seen-ids guard
    tagged: int           # labels successfully applied
    failed: int           # decided, attempted, refused -- each has a FAILED row
    cursor_written: bool  # True ONLY when final_state is COMMIT


class WatchConfig(BaseModel):
    """Operator-set loop parameters. Validated at the type, not by a check."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_seconds: int = Field(default=60, ge=15, le=3600)
    max_pages_per_round: int = Field(default=50, ge=1)
    """Bounds a COLD_START on a large mailbox so one round cannot run forever.
    Hitting it ends the round in ABORTED with NO cursor -- the next round
    re-reads from the same position, which is safe because it is a read."""


def poll_once(
    provider: MailProvider,
    scope: MailboxScope,
    taxonomy: Taxonomy,
    state: WatchState,
    *,
    container_id: str | None,
    classifier: Classifier | None = None,
    config: WatchConfig | None = None,
) -> RoundOutcome:
    """Run ONE round for ONE stream. The whole state table lives here.

    Writes the cursor if and only if the provider returned a final delta link.
    Does not sleep, does not schedule, does not loop over streams -- so a test
    drives every state with no clock and no mailbox.
    """


def poll_mailbox(
    provider: MailProvider,
    scope: MailboxScope,
    taxonomy: Taxonomy,
    *,
    classifier: Classifier | None = None,
    config: WatchConfig | None = None,
) -> tuple[RoundOutcome, ...]:
    """One round across every in-scope stream. Loads and saves WatchState once.

    Gmail yields exactly one outcome (container_id=None); Graph yields one per
    in-scope folder. An out-of-scope Graph folder is NOT POLLED -- it produces
    no outcome because no stream was opened.
    """


def _streams_for(
    provider: MailProvider,
    scope: MailboxScope,
) -> tuple[str | None, ...]:
    """Which streams to poll. THE STREAM-LEVEL SCOPE GATE.

    Gmail: always (None,) -- history.list is mailbox-wide and cannot be scoped
    at the stream, so scope is enforced entirely per message (§4).
    Graph: one folder id per container where scope.covers() is True. An excluded
    folder is absent from this tuple and is therefore never read at all.
    Graph virtual search folders (#microsoft.graph.mailSearchFolder) are
    excluded here -- they are views over other folders and would double-count.
    """


def _process_message(
    message: Message,
    *,
    provider: MailProvider,
    scope: MailboxScope,
    taxonomy: Taxonomy,
    state: WatchState,
    container_id: str | None,
    containers: Mapping[str, Container],
    classifier: Classifier | None,
) -> MessageOutcome:
    """One message, in the fixed order: scope -> replay -> classify -> write
    -> receipt -> mark seen.

    SCOPE PRECEDES CLASSIFY AND THAT ORDER IS NOT NEGOTIABLE -- classification
    reads the message, so an out-of-scope message must be dropped before it,
    not filtered after it (§4).
    """


def _covered(
    message: Message,
    scope: MailboxScope,
    containers: Mapping[str, Container],
) -> bool:
    """Whether the watcher may read this message. Delegates; decides nothing.

    Resolves the message's container ids to names and well_known tags, then
    asks scope.covers_all(). Never re-derives scope from MailboxScope's fields.
    Empty containers -> covers_all returns False -> not covered. Fails closed.
    """


def _record_outcome(
    provider_name: str,
    message_id: str,
    classification: Classification,
    *,
    applied: bool,
    error_code: str = "",
) -> None:
    """Append the receipt. One ledger row per attempted mutation, always.

    applied=False writes Status.FAILED with error_code -- a code, never a
    provider message, because provider prose can quote a subject line.
    """


def _sleep_for_throttle(retry_after_seconds: float | None, attempt: int) -> None:
    """Honour Retry-After exactly; fall back to exponential backoff without it.

    Separate and injectable so the throttle path is testable without a clock.
    """


def stagger_offset(mailbox_id: str, interval_seconds: int) -> int:
    """Deterministic phase offset for this mailbox, in seconds (§6).

    Deterministic, NOT random: a restart must resume the same phase rather than
    re-clustering every mailbox onto the restart instant.
    """


# --- providers/base.py -- ANOTHER LANE'S WORK. Assumed, not specified here.

def tag_message(self, message_id: str, label: str) -> str:
    """Add ``label`` to ``message_id`` WITHOUT moving it. Additive only.

    Gmail: messages.modify addLabelIds, removeLabelIds OMITTED. Append
    primitive -- cannot clobber, no read, no race. 5 QU.

    Graph: GET categories + @odata.etag (BODY field -- there is no ETag
    response header) -> union -> PATCH the FULL set with If-Match. Returns
    early writing NOTHING when the union equals the current set, because a
    content-identical PATCH still bumps the etag.
      412 (ErrorIrresolvableConflict) -> re-GET, re-union, re-PATCH, up to
        retry.MAX_ATTEMPTS. Branch on the STATUS, never the code string.
      400 ErrorInvalidChangeKey -> NEVER retried; it is our own malformed etag.
      Never drop If-Match to force the write through.

    Contrast file_message, which MOVES and removes from the inbox. This method
    exists because the watcher must never do that.
    """
```

---

## 9. Open items before this ships

Ordered by what blocks what.

1. **Confirm `retry.py` treats `403` with a rate-limit reason as retryable.** Gmail
   signals throttling with `403 rateLimitExceeded` as well as `429`, and a
   403-only-fatal policy would fail a healthy Gmail poller. The audit explicitly did
   not check this (`provider-parity-audit.md:355-357`). **Cheapest item here and it
   gates the Gmail lane** — read `is_retryable` and assert it.
2. **Grow `outlook.py`'s `_patch` a `headers` argument** (`outlook.py:628`). `If-Match`
   cannot be sent until it exists; the live test used raw `httpx` to get around it.
3. **Confirm `tag_message`'s landed signature** against §8.4 once the providers lane
   lands it.
4. **Re-check Gmail QU unit costs before hardcoding any.** A 2026-05-01 release note
   says `messages.get` costs changed (`provider-parity-audit.md:337-339`).
5. **Read the Outlook rows of the live throttling-limits table.** Replace the
   UNVERIFIED per-mailbox figures in §6 before any figure reaches a client.
6. **Decide the Gmail nested-label exclusion gap** (§4). Owned by `mailboxscope.py`,
   not by the loop, but the loop is where the exposure would show up.
7. **Confirm Gmail filter-vs-API ordering.** `provider-parity-audit.md:256-260` marks
   the claim that filtered mail still generates a `messageAdded` record as UNVERIFIED
   by direct quote. The over-collection posture in §4 is correct either way; the
   *coverage* claim is what needs the receipt.
8. **Standing watch item, not closeable:** `If-Match` on a message PATCH is
   live-verified but **undocumented** by Microsoft. Behaviour can change without a
   deprecation notice. An unexpected `200` on a stale etag is a signal to re-verify.
