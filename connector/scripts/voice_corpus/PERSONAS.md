# PERSONAS.md — synthetic corpus ground-truth specification

**Status: WRITTEN.** This is a spec, not a corpus. No messages exist yet.
Authored by Sprint 5 Lane A. Baseline commit `807d721`.

This file is the ground truth. A generator lane implements it mechanically; a
measurement lane scores a generated corpus against the numbers here and FAILS
the generation on a miss. Nothing in this file is a matter of taste at
generation time — every trait below is a number with a tolerance.

---

## READ THIS FIRST — THE UNSOUND USE

**This corpus must NEVER be cited as evidence of human voice capture.**

Synthetic prose carries the fingerprints of the model that generated it. A pass
on "does the profiler capture this voice" measures a model recognising its own
output — not voice capture. The circularity is real, and it is sharpest when the
same model family both generates the corpus and profiles it. A profiler could
score perfectly here and be worthless on a real human's mail.

What this corpus IS good for, and the only claims it may support:

1. **Falsification.** Two deliberately opposed voices must yield two
   distinguishable profiles. Clean synthetic is the easiest possible input, so a
   blend here proves failure everywhere. A pass here proves nothing except that
   the floor was cleared.
2. **Mechanics.** Thread parsing, signature stripping, quoted-reply removal,
   greeting extraction — ground truth is an advantage because we wrote the
   answers.
3. **Greeting mirroring (issue #83).** This is the only corpus that will carry
   the INBOUND half of each thread. Neither of the owner's real corpora has it, so
   the mirroring rule is underivable without this.
4. **Volume curve (issue #81).** Where a profile stabilises across
   10/25/50/75/100/200 messages. The widely-repeated "75 emails per author"
   figure is UNVERIFIED — no paper reports it. Measure the curve; do not assume
   a number.

Any lane, doc, README, or report derived from this corpus repeats this warning.

---

## Assumption, not a ruling

The owner did not rule on persona flavour. The terse/verbose opposition below is the
**controller's pick** for Sprint 5, recorded here as an assumption. It was chosen
because terse-vs-verbose is separable on axes a script can count without
judgment, which is what a falsification battery requires. If the owner later rules a
different flavour, the axis table is what changes; the structure of this file is
not affected.

Both personas are INVENTED. Names, addresses, employers, and content are
fictional. Addresses use `example.com` / `example.org` only. No content from the
fluidvoice or rtech-sent corpora appears here or may be used to generate from
this spec.

---

## The two personas

### Persona A — Dana Reyes
`dana.reyes@example.com` · operations lead, Northline Freight (fictional)

Terse and blunt. Short sentences. Minimal or absent greeting. **Never** signs
off. Lowercase sentence starts are acceptable and expected. Sentence fragments
are acceptable. One paragraph, frequently one line. Answers the question and
stops. No courtesy formulae, no hedging, no softening.

### Persona B — Margaret Ellsworth
`m.ellsworth@example.org` · Director of Client Relations, Ellsworth &
Partners (fictional)

Verbose and formal. Long, fully-formed sentences. Full salutation on every
message. Full sign-off block on every message. Multiple paragraphs. Hedging and
courtesy formulae are habitual, not occasional. Contractions are avoided
entirely as a matter of register.

---

## THE NINE-AXIS NUMERIC TABLE

All rates are computed over the message BODY after signature stripping and
quoted-reply removal (the fixtures in the Mechanics section define exactly what
that leaves). Greeting and sign-off lines are EXCLUDED from word counts, sentence
counts, and every per-100-word rate — otherwise Persona B's fixed sign-off block
inflates her formality rate mechanically and the axis stops measuring prose.

Tolerances are **corpus-level**: the measured statistic over the whole ~200-message
set must land inside the band. Per-message variance is expected and is not
measured against these bands.

| # | Axis | Persona A target | A band | Persona B target | B band |
|---|------|------------------|--------|------------------|--------|
| 1 | Median sentence length (words) | 6 ± 2 | [4, 8] | 24 ± 4 | [20, 28] |
| 1a | Sentence length p25 (words) | 4 ± 2 | [2, 6] | 17 ± 4 | [13, 21] |
| 1b | Sentence length p75 (words) | 9 ± 3 | [6, 12] | 32 ± 5 | [27, 37] |
| 2 | Mean sentences per message | 2.5 ± 1.0 | [1.5, 3.5] | 9.0 ± 2.0 | [7.0, 11.0] |
| 3 | Mean paragraphs per message | 1.1 ± 0.2 | [0.9, 1.3] | 3.4 ± 0.6 | [2.8, 4.0] |
| 4 | Messages carrying a greeting (%) | 15 ± 10 | [5, 25] | 100 ± 0 | [100, 100] |
| 5 | Messages carrying a sign-off (%) | 0 ± 0 | [0, 0] | 100 ± 0 | [100, 100] |
| 6 | Hedging markers per 100 words | 0.3 ± 0.3 | [0.0, 0.6] | 3.5 ± 0.8 | [2.7, 4.3] |
| 7 | Contractions per 100 words | 3.2 ± 1.0 | [2.2, 4.2] | 0.2 ± 0.2 | [0.0, 0.4] |
| 8 | Exclamation marks per 100 sentences | 1.0 ± 1.0 | [0.0, 2.0] | 0.2 ± 0.2 | [0.0, 0.4] |
| 9a | Formal markers per 100 words | 0.4 ± 0.4 | [0.0, 0.8] | 4.0 ± 1.0 | [3.0, 5.0] |
| 9b | Informal markers per 100 words | 2.8 ± 0.8 | [2.0, 3.6] | 0.1 ± 0.1 | [0.0, 0.2] |

Rows 1a/1b are the p25/p75 the brief requires for axis 1; they are reported
alongside axis 1 and are not counted as separate axes.

### Disjointness — the falsification battery

Arithmetic, written out, checked band-against-band:

| Axis | A band | B band | Overlap? | Gap |
|---|---|---|---|---|
| 1 median sentence length | [4, 8] | [20, 28] | **NO** | 12 words |
| 2 sentences per message | [1.5, 3.5] | [7.0, 11.0] | **NO** | 3.5 |
| 3 paragraphs per message | [0.9, 1.3] | [2.8, 4.0] | **NO** | 1.5 |
| 4 greeting % | [5, 25] | [100, 100] | **NO** | 75 points |
| 5 sign-off % | [0, 0] | [100, 100] | **NO** | 100 points |
| 6 hedging /100w | [0.0, 0.6] | [2.7, 4.3] | **NO** | 2.1 |
| 7 contractions /100w | [2.2, 4.2] | [0.0, 0.4] | **NO** | 1.8 |
| 8 exclamations /100 sent. | [0.0, 2.0] | [0.0, 0.4] | **YES** | — |
| 9a formal markers /100w | [0.0, 0.8] | [3.0, 5.0] | **NO** | 2.2 |
| 9b informal markers /100w | [2.0, 3.6] | [0.0, 0.2] | **NO** | 1.8 |

**EIGHT of the nine axes are disjoint: 1, 2, 3, 4, 5, 6, 7, 9.** The requirement
was at least six. Axis 9 is disjoint on both of its sub-measures independently.

**Axis 8 (exclamation marks) OVERLAPS on [0.0, 0.4] and is stated as overlapping
rather than fudged.** The tolerance was NOT widened or shifted to manufacture a
gap. The honest reason: neither of these personas is excitable. Dana is blunt,
not enthusiastic; Margaret is formal, and formality suppresses exclamation just
as bluntness does. Both floors sit at zero, so any band that includes zero for
both cannot be disjoint. Making axis 8 separable would require inventing a third
trait ("Dana is peppy") that contradicts the terse/blunt definition, which is a
worse outcome than an honest overlap on one weak axis.

**Consequence for the measurement lane:** axis 8 is measured and REPORTED but is
NOT part of the falsification battery. A discrimination test that relies on
exclamation rate is relying on noise. The battery is axes 1, 2, 3, 4, 5, 6, 7,
9a, 9b — nine disjoint measures across eight axes.

---

## Closed word lists — axes 6, 7, 9

These lists are CLOSED. A measurement lane counts occurrences of exactly these
strings, case-insensitively, on whole-word boundaries. A generator lane may not
introduce a hedge, contraction, or formality marker outside these lists and
expect it to count. Multi-word entries are matched as contiguous phrases.

### Axis 6 — hedging markers (closed list, 20 entries)

```
perhaps
possibly
it seems
it would seem
I would suggest
I would think
I believe
I suspect
somewhat
rather
fairly
to some extent
if I may
I wonder whether
it may be
it might be
arguably
in my view
I am inclined to
one could argue
```

### Axis 7 — contractions (closed list, 24 entries)

```
don't
doesn't
didn't
can't
won't
wouldn't
shouldn't
couldn't
isn't
aren't
wasn't
weren't
haven't
hasn't
hadn't
it's
that's
there's
we'll
we're
we've
I'm
I've
I'll
```

### Axis 9a — formal markers (closed list, 20 entries)

```
regarding
concerning
furthermore
moreover
nevertheless
accordingly
subsequently
therefore
with respect to
in the event that
at your earliest convenience
please be advised
kindly
I trust
should you require
prior to
in order to
provide
request
correspondence
```

### Axis 9b — informal markers (closed list, 20 entries)

```
yeah
yep
nope
ok
okay
thanks
got it
sure
fine
quick
just
stuff
guys
heads up
FYI
ping
grab
sort out
figure out
no worries
```

Note the deliberate asymmetry: "thanks" is on the informal list and is Dana's
only courtesy word. Margaret expresses the same function with "I am grateful
for" or "thank you very much for", neither of which is on either list, so the
lists do not double-count the same intent.

---

## THE INBOUND SIDE — issue #83

Every outbound message in this corpus is a reply to a generated INBOUND message.
The inbound half is generated for the express purpose of making greeting
mirroring measurable, and is the reason this corpus exists at all.

### Inbound greeting distribution

Applied INDEPENDENTLY to each persona's inbound stream. Percentages are of that
persona's inbound messages, and sum to 100. `<name>` is the persona's first name
("Dana" or "Margaret") — inbound correspondents never use surnames or
nicknames, so the greeting form is the only variable.

| Inbound greeting form | Share |
|---|---|
| Bare first name + comma (`Dana,`) | 20% |
| `Hey <name>,` | 20% |
| `Hi <name>,` | 25% |
| `Dear <name>,` | 15% |
| No greeting at all | 20% |
| **Total** | **100%** |

Tolerance on each share: ± 4 percentage points, measured over that persona's
whole inbound set. The shares are deliberately spread rather than uniform so a
mirroring test that simply guesses the modal form does not pass by accident.

### The mirroring rules — A and B differ, and that is the point

If both personas mirrored, or neither did, the inbound half would be decorative
and the test could not distinguish mirroring from a fixed habit. The contrast is
load-bearing.

**Persona A — Dana — DOES NOT MIRROR.** Her outbound greeting is a fixed
function of nothing. Regardless of what arrived:

| Inbound form | Dana's outbound greeting |
|---|---|
| `Dana,` | none (85%) or `<name>,` bare-first-name (15%) |
| `Hey Dana,` | none (85%) or `<name>,` (15%) |
| `Hi Dana,` | none (85%) or `<name>,` (15%) |
| `Dear Dana,` | none (85%) or `<name>,` (15%) |
| none | none (85%) or `<name>,` (15%) |

The 15% is drawn independently of the inbound form. This is what makes axis 4's
target 15%. **Ground truth: Dana's outbound greeting is statistically
independent of the inbound greeting.** A mirroring detector that reports Dana
as a mirrorer has produced a FALSE POSITIVE, and catching that is half the value
of this fixture.

**Persona B — Margaret — MIRRORS, with an escalation floor.** Her outbound
greeting is a deterministic function of the inbound form:

| Inbound form | Margaret's outbound greeting |
|---|---|
| `Margaret,` | `Dear <name>,` |
| `Hey Margaret,` | `Hi <name>,` |
| `Hi Margaret,` | `Hi <name>,` |
| `Dear Margaret,` | `Dear <name>,` |
| none | `Dear <name>,` |

Deterministic — 100%, no probabilistic component. Note she never returns `Hey`:
her floor is `Hi`, and anything at or below `Hey` escalates to `Hi`, while bare
name and no-greeting escalate to `Dear`. **Ground truth: Margaret's outbound
greeting is a deterministic function of the inbound greeting with exactly two
outputs.** A mirroring detector must recover the mapping, not merely observe
that she always greets.

`<name>` in Margaret's outbound is the correspondent's first name. She always
greets, which is why axis 4's target for B is 100%.

---

## MECHANICS FIXTURES — sound use #2

Ground truth is an advantage here: we write the answers, so a stripping test has
one correct output and no judgment call.

### Signature blocks

**Dana's signature.** Appended to **20% ± 5%** of her sent messages. Exact text,
preceded by a blank line and the standard `-- ` delimiter line (two hyphens, one
trailing space — RFC 3676 §4.3):

```
-- 
Dana Reyes
Northline Freight
```

**Margaret's signature.** Appended to **100%** of her sent messages — for her it
is part of the sign-off block, not an optional addition. Exact text, preceded by
a blank line and the same `-- ` delimiter:

```
-- 
Margaret Ellsworth
Director of Client Relations
Ellsworth & Partners
m.ellsworth@example.org | (555) 0100
```

**EXACT EXPECTED OUTPUT AFTER SIGNATURE STRIPPING:** everything from the `-- `
delimiter line to end of message is removed, INCLUDING the delimiter line and
the blank line immediately preceding it. What remains is the body ending at the
last non-empty line before that blank line, with no trailing newline beyond a
single terminating `\n`. Where no signature was appended, stripping is a no-op
and the output is byte-identical to the input.

Note for the measurement lane: Margaret's sign-off (`Best regards,` + name) is
part of the BODY and sits ABOVE the `-- ` delimiter. Signature stripping removes
the delimited block only; it does not remove the sign-off. Axis 5 measures the
sign-off, which survives stripping; the per-100-word rates in the axis table
exclude it separately as stated above.

### Sign-off blocks — the exact allowed sets, axis 5

**Dana — allowed closing blocks: THE EMPTY SET.** She never signs off. 0% of her
messages carry one. A generated Dana message containing any closing block is a
generation failure.

**Margaret — allowed closing blocks, 100% of messages, one drawn per message:**

```
Best regards,
Margaret Ellsworth
```
```
Kind regards,
Margaret Ellsworth
```
```
With appreciation,
Margaret Ellsworth
```

Distribution: `Best regards,` 60% ± 8, `Kind regards,` 25% ± 8,
`With appreciation,` 15% ± 8. The closing line is always followed by her full
name on its own line, then the `-- ` delimiter and signature block.

### Greeting forms — the exact allowed sets, axis 4

**Dana — allowed opening lines:** the empty set (no greeting), or exactly
`<Name>,` (correspondent's first name, capitalised, comma, nothing else) at
15% ± 10.

**Margaret — allowed opening lines:** exactly `Dear <Name>,` or exactly
`Hi <Name>,`, selected by the mirroring table above. No other form.

### Quoted-reply format

Every reply carries the quoted inbound message beneath the body. Exact format:
a blank line, then the attribution line, then the quoted block with each line
prefixed by `> ` (greater-than, one space). Empty quoted lines are prefixed with
`>` and NO trailing space.

Attribution line, exact format:

```
On Tue, 12 Aug 2026 at 09:14, Alex Turner <alex.turner@example.com> wrote:
```

That is: `On `, RFC-5322-style day-of-week and date, ` at `, `HH:MM`, `, `,
display name, ` <`, address, `> wrote:`.

Full worked example of a Dana reply as it appears on the wire:

```
tuesday works. 2pm.

-- 
Dana Reyes
Northline Freight

On Tue, 12 Aug 2026 at 09:14, Alex Turner <alex.turner@example.com> wrote:
> Hi Dana,
>
> Are you free Tuesday to walk through the dock schedule?
>
> Alex
```

**EXACT EXPECTED OUTPUT AFTER QUOTED-REPLY REMOVAL** (signature retained, to
show the two operations are independent):

```
tuesday works. 2pm.

-- 
Dana Reyes
Northline Freight
```

**EXACT EXPECTED OUTPUT AFTER BOTH QUOTED-REPLY REMOVAL AND SIGNATURE
STRIPPING** — this is the body the axis table measures:

```
tuesday works. 2pm.
```

Removal rule stated precisely: delete the attribution line, every subsequent
line, and the blank line immediately preceding the attribution line. The
attribution line is identified by matching `^On .* wrote:$`; the `> ` prefixed
block is not itself the match target, because a quoted block can be present
without a prefix in some clients and the attribution line is the reliable
anchor.

Nesting depth is exactly ONE for this corpus. No reply quotes a reply that
quotes a reply. That is a deliberate simplification of a bench fixture, and a
stripper that only handles depth 1 will pass here and may still be wrong on real
mail — a limitation to state, not to fix by gold-plating this corpus.

---

## Corpus scale

- ~200 sent messages per persona (400 sent total).
- Plus the INBOUND half of every thread: one inbound message per sent message,
  ~400 inbound total.
- Thread LENGTH varies: ~60% two messages (one inbound, one reply), ~30% four,
  ~10% six. **CONTROLLER RULING 2026-08-20**, resolving a conflict between this
  section (which originally said every thread is two messages deep) and the
  lane-B brief (which specified 60/30/10). The brief wins, because multi-message
  threads are what exercise quoted-reply removal, and that is sound use #2 of
  this corpus — a corpus of uniformly two-message threads cannot test it at all.
- QUOTING NESTING DEPTH remains exactly ONE regardless of thread length: each
  reply quotes only its immediate predecessor, never an already-quoted block.
  The depth-1 note above still stands in full, including its limitation — a
  quoted-reply stripper that only handles depth 1 will pass this corpus and may
  still be wrong on real mail.
- CONSEQUENCE for any measuring lane: outbound messages per persona are ~306,
  NOT ~200. Thread count is exactly 200 per persona. A check that hard-asserts
  ~200 messages per persona will fail on a corpus that is correct.
- Volume-curve sampling points, for issue #81: 10 / 25 / 50 / 75 / 100 / 200
  messages per persona. The 200-message target exists to make the 200-point
  measurable; it is not a claim that 200 is the right number.

## Scope

This is a bench fixture: it exists to test and validate the idea, not to be
perfect. Do not gold-plate it. Every simplification above (depth-1
threads, closed word lists, two sign-off variants) is deliberate.
