# Backend Requirements — what AI runs this, and what happens to the mail

**Status of this document: WRITTEN.** It is a requirements and evidence
document, not a deployment record. Every benchmark result cited below is
RUNNING (executed live, receipts on disk, file named at each claim). Every
recommendation is PROPOSED. Where Microsoft does not disclose something, this
says **not disclosed**; where a widely-repeated figure could not be confirmed
against a primary source, it says **UNVERIFIED** rather than repeating it.

Written 2026-08-19.

This document is written for two readers at once, and they want different
things:

- **The MSP or IT security reviewer** deciding whether to allow this in a
  client environment. Start at §5 (what happens to the data) and §3 (Microsoft).
  §1 is the technical contract the software actually enforces.
- **The business owner** deciding what to buy. §2 is the short answer, §6 is the
  table that tells you which one is yours. You do not need §1.

---

## §0. The short version

The connector sorts mail into folders. To do that it asks a language model, one
message at a time, "which of these nine folders does this belong in?" — sending
**three fields only: sender, subject, and the provider's own short preview
snippet**. It never sends the body of an email, because it cannot: the object it
holds has no body field at all
(`src/inbox_zero_connector/classifiers/openai_compatible.py:_message_facts`).

You supply the model. rTech does not host it, does not resell it, and does not
put itself in the path of your mail. Two postures are supported, and they are
system requirements rather than preferences:

1. **A corporate-tier AI account with a no-training data agreement** — Teams,
   Enterprise, an API account under a business agreement, or Azure OpenAI in
   your own tenant.
2. **Local inference on hardware you already own**, under 8GB of RAM. No
   subscription, no account, and mail content never leaves the building.

**Consumer AI subscriptions are excluded.** Not on capability — on data terms.
See §2.3.

---

## §1. What the connector requires of a backend

There are **two independent gates**. A backend must clear both. They are never
blended into one score, because a model can be brilliant and still fail the
first one, and no amount of accuracy repairs that.

### Gate 1 — Protocol: exactly one valid `tool_calls` response

The connector does not read prose. It requires the model to answer by *calling a
function* named `file_message` with three arguments (`folder`, `confidence`,
`reason`), returned in the standard `tool_calls` field of the response. When a
model answers any other way, the connector **raises an error and files nothing**
— it does not fall back to parsing the text.

This is deliberate and is stated in the source
(`src/inbox_zero_connector/classifiers/openai_compatible.py`, module docstring):

> A parser that usually works is worse than one that never runs: it succeeds
> through testing, then misfiles mail on the first response shaped slightly
> differently, and the failure looks like a bad classification rather than a
> broken integration.

The enforcement is in `_extract_arguments()` in that file, which raises
`ClassifierBackendError` on: no choices, no tool call, the wrong function name,
arguments that are not valid JSON, and arguments that fail schema validation —
five separate guards with five separate messages.

**This gate is hard at any intelligence level, and a capable model can fail it.**
The proof is on disk. `Qwen3-Coder-30B-A3B-Instruct-4bit` — a large, competent
coding model — failed **all 35 of 35** messages with `error: "no_tool_call"`. It
knew the right answer and emitted it in the wrong envelope, as pseudo-XML in the
content field. Verbatim from the first row of
`bench_results.json` → `Qwen3-Coder-30B-A3B-Instruct-4bit[0].content`:

```
<function=file_message>
<parameter=folder>
Business-Action
</parameter>
<parameter=confidence>
0.9
</parameter>
...
```

Its folder choice, `Business-Action`, was **correct**. It is disqualified
anyway. That is the gate doing its job: this backend would misfile mail the
moment its output shape drifted, and it is better to refuse it up front.

*Evidence state: RUNNING — 35/35 `no_tool_call`, verified by re-reading
`bench_results.json` directly for this document.*

**What clears Gate 1, measured:** Luna 35/35, DeepSeek 35/35
(`bench_luna.json`), Qwen3-4B local 35/35 (`bench_results.json`).

**What is known to fail Gate 1:** any text-only endpoint, including the
Microsoft 365 Copilot Chat API (§3.1), and `Qwen3-Coder-30B` as tested.

### Gate 2 — Accuracy: the floor

Accuracy is scored on a fixed 35-message corpus (`fourway.json`) against a
reviewed answer key, with the same prompt and the same tool schema for every
backend. "Exact" means the model picked the same folder a human reviewer did.

**The floor is 80% exact.** The evidence that a cheap commodity tier clears it
is Luna at **86%** (§4).

Two things a reviewer should understand about this number before reading it as
low:

- **Some of the missing percentage is the corpus, not the model.** The answer
  key itself was corrected during review, and at least one item is genuinely
  arguable between two folders. A perfect model does not score 100% here.
- **Low confidence is a correct answer, not a miss.** The connector coerces
  anything below its confidence floor into a `Review` folder for human eyes,
  and the system prompt explicitly instructs the model to prefer an honest low
  confidence over a confident guess. The number that actually matters for risk
  is not "how often was it wrong" but "how often was it wrong *and sure of
  itself*" — the wrong-at-high-confidence column in §4, because that is the
  error class human supervision cannot catch.

### What the connector does NOT require

Worth stating, because it shortens the shopping list:

- **Not a frontier model.** The task is a nine-way choice over three short
  fields. It is instruction-following, not reasoning.
- **Not a specific vendor.** The classifier module is named for the *wire
  protocol*, not a vendor. Switching backends is a base URL and a model ID in
  config — **no code changes**. OpenAI, Azure OpenAI, OpenRouter, and local
  servers (Ollama, LM Studio, vLLM, MLX) all serve this same shape.
- **Not statefulness.** Every call is an independent, stateless chat completion.
  The connector uses no Assistants threads, no stored completions, no file
  uploads, no server-side conversation state. This meaningfully shrinks the
  security review — see §5.

---

## §2. The two supported postures

These are **system requirements**. A deployment that matches neither is not
supported, and that is a data-terms decision rather than a technical one.

### 2.1 Posture A — Corporate-tier account with a no-training data agreement

Any of the following qualifies:

- A **Teams or Enterprise** tier of a commercial AI service, whose business
  terms commit that customer content is not used for training.
- An **API account under a business agreement** (a commercial terms of service
  or DPA, not the consumer click-through).
- **Azure OpenAI in the client's own tenant** — the strongest version of this
  posture, and the one a security reviewer can partly *verify* rather than
  trust (§5.3).

What you are buying with the corporate tier is not a better model. In several
cases it is literally the same model. You are buying the **data agreement**.

### 2.2 Posture B — Local inference, under 8GB RAM

No subscription. No account. No vendor in the path at all. The model runs on
hardware the client already owns, and mail content never crosses the network
boundary.

For a buyer whose objection is storage and exfiltration, this is a categorically
stronger answer than any retention policy, and the source says why:

> A policy is a commitment someone can change; a network boundary is a fact.
> — `classifiers/openai_compatible.py`, module docstring

**The measured point:** `Qwen3-4B-Instruct-2507-4bit`, roughly **2.4GB** on
disk in 4-bit quantization, scored **77% exact / 91% soft, 35/35 protocol, 1.48s
mean latency** (`bench_results.json`, re-verified for this document). That is
below the 80% floor on exact match while clearing protocol perfectly and running
~2x faster than the cheapest hosted option.

**The honest gap, named rather than papered over:** the **4GB–6GB band is
UNMEASURED as of this writing.** We have a data point at ~2.4GB (77%) and data
points from hosted frontier models (83–86%). We have **no valid measurement of
anything in between**, and the models most likely to sit exactly at the 80%
floor live in that band. A client choosing Posture B on the strength of this
document is choosing the 4B's 77%, or is choosing to fund the measurement.

**Two further cautions on Posture B, from the same runs:**

- The 4B was **wrong at confidence ≥ 0.95 seven times out of 35** — the worst
  calibration of anything measured. Its errors do not announce themselves, so
  the `Review` folder does not catch them. One of those seven filed a personal
  household insurance notice as **Business-Reference at 0.95** — a
  business/personal boundary crossing, which for a professional-services client
  is the error class that matters most (`bench-luna.md` §4). Neither hosted
  model reproduced it.
- Model size is not the only local constraint; **serving capacity is**. In the
  original local runs, the 30B and 35B models produced *zero* usable rows — 29
  and 35 HTTP errors respectively (507 Insufficient Storage and 400 Bad
  Request) against a laptop-class memory ceiling (`bench_results.json`). Local
  means sizing the box, not just downloading the weights.

### 2.3 Excluded: consumer subscriptions

**A consumer-tier AI subscription is not a supported backend.** This exclusion
is on **data terms, not capability** — the model behind a consumer plan is
frequently the same model behind the business plan, and it would pass both gates
in §1.

The reason is that consumer agreements generally permit the provider to train on
the content submitted, and the content here is client mail. That is precisely
the thing under security review. A backend that clears every technical gate and
fails the data agreement is still excluded, and the exclusion is not negotiable
per-client on the grounds that the model is good.

*State: PROPOSED as product policy — this is rTech's stated requirement, and a
reviewer should still read the specific agreement for the specific consumer
product in question rather than take this paragraph as a legal finding.*

---

## §3. Microsoft specifically

Every Microsoft 365 client asks "can we just use Copilot, we already pay for
it?" The answer is **no for Copilot, yes for Azure OpenAI**, and the reason is
the protocol gate rather than anything about quality or openness.

Source for this whole section: `copilot-backend-research.md`, research date
2026-08-19, primary sources only. Doc URLs are given inline.

Note that "Copilot" is four unrelated products. Only two are worth the reader's
time here; GitHub Copilot (IDE code completion) and Copilot Studio (a
conversational bot channel, wrong shape — it returns bot activities, not a
`tool_calls` object) are dismissed.

### 3.1 Microsoft 365 Copilot — fails the protocol gate

There **is** now an API. A **Copilot Chat API** shipped in preview, under the
Graph namespace at `graph.microsoft.com/beta/copilot`, with normal Entra ID app
registration and OAuth.

- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/chat/overview
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/copilot-apis-overview

It cannot serve as this connector's backend. From Microsoft's own **Known
Limitations**, verbatim:

> - The Chat API only responds with textual responses.
> - The Chat API doesn't support tools such as code interpreter and graphic art.

And restated in the APIs overview:

> The Chat API returns textual responses; it answers questions and provides
> guidance but doesn't perform actions.

**This is Gate 1, failed.** The connector requires exactly one valid `tool_calls`
response and raises without one. A text-only endpoint cannot satisfy that
contract, and the only way to bolt it on would be a prose parser — the exact
failure mode the design rejects (§1).

**It is a protocol failure, not an intelligence failure.** M365 Copilot may well
be smarter than anything else on this page. It is disqualified regardless, which
is the clearest demonstration available of why the two gates in §1 are kept
separate.

Two further blockers, even if the protocol matched:

- **Licensing.** Microsoft: *"The Chat API is available at no extra cost to
  users with a Microsoft 365 Copilot add-on license. Support for users without a
  Microsoft 365 Copilot add-on license isn't currently available."* That is a
  per-user Copilot add-on on top of E3/E5, for every mailbox classified.
- **It is not a raw model.** *"The Chat API uses both enterprise search
  grounding and web search grounding by default."* For a nine-way folder
  decision over three short fields, RAG-grounding every call against the
  tenant's entire semantic index is wrong-shaped and adds latency.

### 3.2 Which model backs Copilot is not disclosed, and changes

This matters for a security review that wants to pin a posture to a model.

Microsoft names *categories* of provider, never a pinned version:

> Microsoft offers third-party AI models in Microsoft Copilot, such as Anthropic
> and OpenAI models. As an admin, you can decide whether to use these models…

And explicitly on change:

> The AI models that power Microsoft Copilot are regularly updated and enhanced.

- https://learn.microsoft.com/en-us/microsoft-365/copilot/microsoft-365-copilot-privacy
- https://learn.microsoft.com/en-us/microsoft-365/copilot/ai-models-overview

**Do not accept "Copilot uses model X" in a security document.** It is unpinnable
by design.

Two specific findings for a reviewer:

- Copilot may route to **Anthropic models as a subprocessor**, and Microsoft
  states those are *"currently excluded from the EU Data Boundary."* If the
  client is EU-touching, that is a concrete finding, not a footnote.
- Admins can toggle third-party models on or off in the M365 admin center.

Azure OpenAI has the opposite property: the customer picks an exact model and
pins it to a named deployment.

### 3.3 Azure OpenAI — the workable Microsoft-hosted answer

Runs in the **customer's own Azure subscription**, speaks the OpenAI
chat-completions shape, and clears both gates.

- **Gate 1, protocol: cleared, documented.** Function calling with
  `strict: true`, plus JSON-Schema-constrained structured outputs, on both Chat
  Completions and the Responses API. GA from API version `2024-10-21`.
  Schema constraints to respect: the root object cannot be `anyOf`, and all
  fields must be listed as `required` — this connector's three-field schema
  (`folder` / `confidence` / `reason`) satisfies both trivially, since it
  already declares all three required with `additionalProperties: false`.
  - https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs
- **Gate 2, accuracy: not measured on our corpus.** See §4.3 — this document
  will not invent a number for it.
- **Integration cost: a base URL and a model ID.** No code changes. The
  classifier module names Azure OpenAI explicitly as a substitution.
- **Access:** *"all Azure customers are eligible for access to Models sold by
  Azure"* — no registration form for normal use. A form is required only to
  access a Limited Access model or to **modify content filters or abuse
  monitoring** (§5.3, and that one matters).
  - https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/limited-access
- **Residency is a deployment-type choice** and is load-bearing for a
  residency-sensitive client: **Standard** (customer-specified geography),
  **DataZone** (anywhere in the named data zone), **Global** (any geography
  where the model runs). Pick Standard/regional to keep processing in-geo.
  - https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy

**Region × model availability is UNVERIFIED** — the catalog is regionally gated
and changes. Check the client's actual subscription against
https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/models before
promising a specific model in a specific region.

---

## §4. The measurements

**All state: RUNNING.** Every row below was executed live and its receipts are
on disk in the named file. Each number in this section was independently
re-derived from the raw JSON while writing this document, not copied from a
summary.

**Methodology, held constant across every row:** the same 35-message corpus
(`fourway.json`), the same system prompt, the same tool schema, the same
`temperature = 0.0`, the same nine-folder taxonomy. The benchmark harness
imports the shared prompt and schema *as the same objects*, not as copies — only
the transport differs between backends (`bench-luna.md`, harness note).

"Exact" is scored against the **corrected** answer key (`gt_corrected` in
`fourway.json`, present for all 35 items). "Soft" additionally counts a
documented acceptable alternate folder.

### 4.1 The valid results

| Backend | Exact (corrected key) | Soft | Protocol | Wrong @ ≥0.95 | Latency (mean) | Source |
|---|---|---|---|---|---|---|
| **Luna** (cheap routed tier) | **30/35 = 86%** | 34/35 = 97% | **35/35** | **4** | **2.88s** | `bench_luna.json` |
| **gpt-5.6-terra** | 86% | — | — | 1 | — | `bench-luna.md` |
| **DeepSeek** (hosted) | **29/35 = 83%** | 33/35 = 94% | **35/35** | **0** | 6.25s | `bench_luna.json` |
| **Qwen3-4B local** (~2.4GB) | **27/35 = 77%** | 32/35 = 91% | **35/35** | **7** | **1.48s** | `bench_results.json` |

Reconciliation note, in the interest of the numbers being checkable: an ad-hoc
re-score written for this document initially produced 97% soft for DeepSeek and
94% for the 4B, because it counted a match against the *original* key as soft in
addition to the corrected key and the alternates. The source files score soft as
corrected-key-plus-alternates only. **The source figures are the ones in the
table above**, and they are the ones to cite.

### 4.2 What the table means for each gate

**Gate 1 (protocol) is clean across all four.** 35/35 for Luna, DeepSeek, and
the local 4B. Protocol compliance is not correlated with model size or price —
it is a property of the serving implementation, which is exactly why it is a
separate gate. The 30B coder that failed it (§1) is larger than the 4B that
passed it.

**Gate 2 (accuracy):**

- **Luna at 86% is the evidence that the cheap tier of a commodity route clears
  the floor.** This is the load-bearing result of the whole exercise. It matches
  gpt-5.6-terra's 86% and beats the local 4B by 9 points. The floor does not
  require anyone to buy a frontier tier.
- **DeepSeek at 83% also clears**, one message behind Luna. On a 35-item corpus
  the difference between 30 and 29 is a single message, which is noise. Note
  that `bench-luna.md` is explicit that this run **did not reproduce** a prior
  cited 89% for DeepSeek, and states its own 83% as this run's number rather
  than a refutation — different transport and provider-side routing, and
  `temperature 0.0` does not make a hosted mixture-of-experts model
  deterministic.
- **The local 4B at 77% does not clear the 80% floor**, while being the fastest
  thing measured.

**The calibration column is the one a security reviewer should read**, because
it counts errors that do not announce themselves and therefore route straight
past the `Review` folder:

- **DeepSeek: 0.** Every one of its misses landed between 0.55 and 0.85 —
  low enough for a confidence threshold to catch. Its worst miss came in at 0.6;
  it announced its own doubt.
- **Luna: 4.** This is Luna's real weakness and the number to state alongside
  the 86%. Three of the four are the same Receipts-vs-Action confusion — a
  money-movement distinction, not a confidentiality one. The fourth chose
  `Review`, which is the human-supervision folder, i.e. a *safe* wrong answer.
- **Qwen3-4B: 7.** The worst of the four, and the reason it is not simply "the
  cheap option that is slightly less accurate."

**On the confidentiality axis specifically** — business mail landing in a
personal folder or the reverse — `bench-luna.md` §4 reports **Luna 0 crossings
out of 35** and **DeepSeek 1 of 35** (a personal item dropped, not leaked across
the business boundary). The local 4B produced the one crossing that matters: a
personal insurance notice filed Business-Reference at 0.95 confidence. Both
hosted models filed it correctly.

**Plain statement of the tradeoff:** Luna is better at being right; DeepSeek is
better at knowing when it isn't. If the `Review` folder is the entire trust
model, DeepSeek's calibration is the better property even at 3 points lower
exact accuracy and 2.2x the latency.

### 4.3 What is NOT capability data — invalid by methodology

Three local runs appear in `bench_results.json` and **must not be cited as
capability results**, in either direction:

- `Qwen3.8-27B-4bit` — an 86% figure circulates for this model. It is scored
  over only the rows that survived, after 6 of 35 requests errored out
  (`IncompleteRead` ×3, `HTTPError` ×3), at ~96s mean latency. **Invalid by
  methodology.**
- `Qwen3-30B-A3B-4bit` — 29 of 35 rows failed with HTTP 507 Insufficient
  Storage. **No usable data.**
- `Qwen3.6-35B-A3B-4bit` — 35 of 35 rows failed with HTTP 400. **Zero usable
  rows.**

These runs were executed concurrently against a laptop-class memory ceiling.
Their numbers are artifacts of resource exhaustion, not measurements of the
models. A separate `bench_27b_fair.json` re-run exists on disk to address
exactly this, and **it is incomplete** — it contains a single row, and that row
is itself an error (`oMLX prefill memory guard rejected this prompt`). It
therefore contributes **no data at all** to this document.

**Consequence for §2.2:** because these three are excluded, there is no valid
local measurement above ~2.4GB. That is the source of the unmeasured 4–6GB band,
and it is a gap in the evidence rather than a gap in the writing.

### 4.4 Azure OpenAI on this corpus: not measured

No GPT-class Azure deployment has been run against this corpus. **No number is
given here**, and one should not be inferred into a client document.

What can be said, labelled as inference: this is short-context
instruction-following, the observed spread between a 4B and two hosted frontier
APIs is only ~9 points exact, and part of the remaining error is label
ambiguity that no model resolves. A `gpt-4.1`/`gpt-5`-class deployment would
*plausibly* land in the same 83–86% band. **That is a hypothesis to test, not a
result to publish**, and the test is cheap — the benchmark harness already
exists and switching it to Azure is a base URL and a model ID.

---

## §5. What happens to the data

**This is the section the security reviewer should read first.**

### 5.1 What leaves the client's control, per message

**Three fields. Sender, subject, and the provider's own preview snippet,
truncated to 500 characters.** Nothing else.

This is a property of the code rather than a promise about its behaviour, and
the distinction is worth a reviewer's attention:

- The fields are **enumerated individually by name** in `_message_facts()`
  (`classifiers/openai_compatible.py`), not serialized from an object. A future
  field added to the message model cannot start being transmitted without
  someone editing that function. The source states the reason: a model dump
  *"would ship whatever field `Message` grows next, silently, and the first
  anyone would know is when a field that mattered had already been leaving the
  building."*
- **There is no body to send.** The `Message` object carries no body field at
  all; bodies are reachable only through a separate `get_thread` call which this
  module never makes. So "we send a snippet, not the mail" describes what this
  code *can reach*, not what it chooses to send.
- The 500-character snippet cap bounds two exposures at once: how much client
  mail leaves per call, and how much attacker-controlled text a hostile sender
  can put in front of the model.

**Prompt injection is treated as expected, not exceptional.** The system prompt
instructs the model that message text is untrusted data written by the sender
and must never be followed as an instruction. Independently of the prompt, the
model's answer is validated against the installed folder vocabulary downstream —
an out-of-taxonomy folder is coerced to `Review` with the original suggestion
preserved in the ledger, deliberately *not* sanitized at the classifier, because
a model proposing an illegal folder is the signal an injection produces and
hiding it would destroy that signal.

### 5.2 What the connector itself retains

**The posture is ephemeral by default: content is read, sorted, discarded.**

The only persisted state is:

- the **voice profile**, and
- the **sorting rules**.

Neither is message content. Storage beyond that is a **client opt-in**, not a
default.

Consequences a reviewer can bank:

- Every backend call is a **stateless chat completion**. No server-side
  conversation state, no Assistants threads, no stored completions, no file
  uploads. For Azure specifically this removes an entire category from the
  review (§5.3).
- A backend failure produces a **FAILED receipt in the ledger and no filing** —
  the connector raises rather than guessing a folder, so an outage is
  distinguishable from a mailbox full of genuinely ambiguous mail.
- Logging is content-bounded by construction: model IDs, base URLs, and model
  reasons are truncated before they can reach a log line, and the codebase has
  tests enforcing the `key=value` call-site discipline that the redaction pass
  matches on.

### 5.3 Azure OpenAI: what Microsoft commits, and what a reviewer can verify

Source: https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy
and https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/abuse-monitoring

**The commitments**, verbatim:

> Your prompts (inputs) and completions (outputs), your embeddings, and your
> training data:
> - are NOT available to other customers.
> - are NOT available to OpenAI or other providers of Models sold by Azure.
> - are NOT used by providers of Models sold by Azure to improve their models or services.
> - are NOT used to train any generative AI foundation models without your permission or instruction.

On "analyze but do not store" specifically:

> The models are stateless: no prompts or completions are stored in the model.

On egress:

> Microsoft hosts the Models sold by Azure in Microsoft's Azure environment and
> Models sold by Azure do NOT interact with any services operated by providers
> of Models sold by Azure, for example, OpenAI (e.g. ChatGPT, or the OpenAI API).

Anything stored by *optional stateful features* (Responses API, Assistants
threads, stored completions, uploaded files) is stored at rest in the customer's
own Azure tenant, AES-256 encrypted with optional customer-managed keys, and
deletable at any time. **This connector uses none of those features**, so the
whole category is out of scope — state that in the review as a scope reduction.

**The abuse-monitoring asterisk, which is the detail that surprises people.**
By default, flagged prompts and completions may be sampled and stored for
review. Automated review does **not** store: *"prompts and completions that
undergo such review are not stored by the abuse monitoring system or used to
train the AI model."* Human review **does** store — per-geography, logically
separated by customer resource ID, reachable only by authorized Microsoft
employees via point-wise queries by request ID, from Secure Access Workstations,
under just-in-time manager-approved access.

It **can** be turned off:

> Microsoft allows customers who meet additional Limited Access eligibility
> criteria to apply to modify abuse monitoring… If the customer has been
> approved for modified abuse monitoring, the data storage and human review
> process described above is not performed.

**The catch, and it is a real one for this client base:** eligibility is limited
to *"customers and partners managed by a Microsoft account team or under an
eligible program."* **A small business very likely does not qualify.** Plan on
the assumption that **abuse monitoring stays ON**, and treat getting it off as an
upside rather than a prerequisite. Some advanced models carry stricter criteria
still.

**Retention period: UNVERIFIED.** The widely-repeated "30-day" figure **was not
found** on either the abuse-monitoring page or the data-privacy page when checked
on 2026-08-19. **Do not put a day count in a client document.** If a number is
needed, re-check the current Product Terms and cite them directly.

**The reviewer can VERIFY rather than trust.** This is the strongest single fact
in this document. Microsoft documents the check under "How can a customer verify
if data storage for abuse monitoring is off?":

```
az cognitiveservices account show -n <resource_name> -g <resource_group>
```

Returns, when logging is off:

```json
{ "name": "ContentLogging", "value": "false" }
```

> The value of "false" for the "ContentLogging" attribute appears only if data
> storage for abuse monitoring is turned off. Otherwise, this property will not
> appear.

Also visible in the Azure portal via the resource Overview → JSON view. Note the
asymmetry carefully: **the flag's absence is the default state, and its presence
is the proof.** Everything else — statelessness, no-training — remains
**contractual**: DPA and Product Terms, backed by audit certifications (ISO
27001, ISO 42001, HIPAA, GDPR), not independently inspectable by the customer.

### 5.4 M365 Copilot's data posture, for completeness

Copilot cannot be this connector's backend (§3.1), but reviewers will ask how it
compares, and on one axis it is genuinely better:

> While abuse monitoring, which includes human review of content, is available
> in Azure OpenAI, **Microsoft Copilot services have opted out of it.**

No application, no account-team gate. If the objection is specifically *human
review*, Copilot is already where Azure needs an approval to get.

But it **stores more, by design**: user prompts and Copilot responses are
retained as the user's Copilot activity history. That store is inside the
tenant, encrypted at rest, admin-managed, and **auditable** — Content search and
Microsoft Purview expose it, Purview retention policies govern it, and users can
delete their own history. For some reviewers, an admin-visible audit trail is a
feature rather than a liability.

- https://learn.microsoft.com/en-us/purview/retention-policies-copilot
- https://learn.microsoft.com/en-us/purview/ai-microsoft-purview

Boundary caveats worth naming: web-search grounding sends a generated query to
**Bing**, outside the tenant (admin-controllable via the connected-experiences
privacy control,
https://learn.microsoft.com/en-us/microsoft-365-copilot/manage-public-web-access);
LLM calls route to the nearest data centers *"but also can call into other
regions where capacity is available"*; and **Anthropic subprocessor models are
excluded from the EU Data Boundary**. Verification is weaker than Azure's —
there is no `ContentLogging`-equivalent flag to read, so the abuse-monitoring
opt-out is contractual only.

### 5.5 Side-by-side

| | Local (Posture B) | Azure OpenAI | M365 Copilot Chat API |
|---|---|---|---|
| Usable as our backend | **Yes** | **Yes** | **No** — text only, no tools |
| Content leaves the network | **Never** | Three fields per message | n/a |
| Model disclosed and pinnable | **Yes** — you chose the file | **Yes** — customer pins the deployment | **No** — changes, may be Anthropic |
| Used for training | No party involved | No | No |
| Model stores content | No | No — *"models are stateless"* | No |
| Service stores content | Nothing external exists | Only via optional stateful features — **we use none** | **Yes** — activity history, by design |
| Human abuse review | None | On by default; off only via managed-customer application | **Already opted out** |
| Reviewer can VERIFY | Network boundary — inspectable directly | **`az cognitiveservices account show` → `ContentLogging: false`** | Purview audit logs; opt-out is contractual |
| Residency | The client's building | Deployment type — pick **Standard/regional** | EUDB for EU; may cross regions otherwise |

---

## §6. Recommendation — which backend, given the situation

**State: PROPOSED.** These are recommendations, not measured outcomes. The
accuracy column carries the state of the evidence behind each.

| The client's situation | Recommended backend | Accuracy evidence | Why |
|---|---|---|---|
| Microsoft 365 shop, has an MSP, security review required | **Azure OpenAI**, Standard/regional deployment, in their own tenant | Not measured on our corpus (§4.4); protocol documented | The only Microsoft answer that clears the protocol gate. Customer pins the model, controls residency, and can **run a command** to check the one storage asterisk. |
| "Just use the Copilot we already pay for" | **Not supported — Azure OpenAI instead** | n/a | Chat API is text-only and does not support tools (§3.1). Protocol failure, not a quality judgment. Also needs a per-user add-on license for every mailbox. |
| Confidentiality is the whole objection; mail must not leave the building | **Local inference, Posture B** | Qwen3-4B: **77% exact**, 35/35 protocol, 1.48s (RUNNING) | A network boundary beats a retention policy. Go in knowing it is **below the 80% floor** and has the worst calibration measured (7 wrong at ≥0.95), so the `Review` folder catches less. The 4–6GB band that might clear the floor is **UNMEASURED**. |
| Cost-sensitive, no Azure, wants the cheapest thing that actually works | **A cheap routed commodity tier (Luna-class)** | **86% exact**, 35/35 protocol, 2.88s (RUNNING, `bench_luna.json`) | The floor does not require a frontier tier. Requires Posture A — a corporate/business-terms account, never a consumer plan. State the 4 wrong-at-≥0.95 alongside the 86%. |
| `Review` folder is the entire trust model; a confident misfile is the feared failure | **DeepSeek-class hosted** | **83% exact**, 35/35 protocol, **0 wrong at ≥0.95**, 6.25s (RUNNING) | Trades 3 points of exact accuracy and 2.2x latency for errors that reliably announce themselves. Every miss landed at ≤0.85, i.e. catchable by a confidence threshold. |
| High mailbox volume, latency matters | **Luna-class** hosted, or local 4B if content cannot leave | 2.88s vs 1.48s mean (RUNNING) | Classification runs once per message, so latency multiplies by inbox size. DeepSeek's 6.25s mean and 14.78s max are the outlier here. |
| EU-touching data residency requirement | **Azure OpenAI, Standard/regional** — or local | Not measured on our corpus | Deployment type controls processing geography. Copilot may route to Anthropic subprocessor models **excluded from the EU Data Boundary** (§3.2). |
| Consumer ChatGPT/Claude/Gemini subscription | **Not supported** | n/a — excluded on terms, not capability | Consumer agreements generally permit training on submitted content, and the content is client mail (§2.3). |

### The one-line version for each reader

**For the business owner:** you need either a business-tier AI account (not the
consumer plan you may already have) or a spare computer. The cheap tier of a
normal AI service is measured at 86% and is enough — you are not required to buy
the expensive one.

**For the security reviewer:** three fields per message, never the body, no
persistence of content by the connector, stateless calls only, no optional
stateful features in use. On Azure the one storage asterisk is default abuse
monitoring, which for a small business will probably stay on — and whose state
is a command you can run yourself rather than a promise you have to accept.

---

## §7. Open items

Named so they are not mistaken for settled:

1. **The 4–6GB local band is UNMEASURED.** The models most likely to sit exactly
   at the 80% floor while staying under 8GB RAM have no valid data point. The
   existing 27B/30B/35B runs are invalid by methodology (§4.3) and the "fair"
   re-run is a single errored row.
2. **Azure OpenAI has never been run against this corpus** (§4.4). The harness
   exists; the run is a base URL and a model ID away.
3. **The abuse-monitoring retention period is UNVERIFIED** (§5.3). No day count
   belongs in a client document until it is re-checked against current Product
   Terms.
4. **Region × model availability on Azure is unverified** and changes; check the
   client's actual subscription.
5. **The 80% accuracy floor is a stated product threshold, not a derived one.**
   It is defensible against the measured spread but has not been tied to a
   measured business outcome (e.g. how many misfiles per week an owner tolerates).

---

## Sources

Benchmark receipts, all executed live, all against the same 35-message corpus:

- `bench_luna.json` — Luna and DeepSeek rows (per-message folder, confidence,
  latency, tool-call count). Numbers in §4.1 re-derived from this file.
- `bench-luna.md` — the Luna/DeepSeek analysis, including calibration breakdown
  and business/personal axis crossings.
- `bench_results.json` — the original five-model local run: the valid Qwen3-4B
  rows, and the `no_tool_call` / HTTP-error rows of §1 and §4.3.
- `fourway.json` — the 35-message corpus and its answer key, including
  `gt_corrected` for all 35 items.
- `bench_27b_fair.json` — **incomplete**: one row, and that row is an error.
  Contributes nothing.

Microsoft: every claim in §3 and §5.3–5.4 carries its `learn.microsoft.com` URL
inline. Research date 2026-08-19, primary sources only
(`copilot-backend-research.md`).

Connector behaviour: `src/inbox_zero_connector/classifiers/openai_compatible.py`
— the module docstring is the design rationale, `_message_facts()` is the data
boundary, and `_extract_arguments()` is the protocol gate.
