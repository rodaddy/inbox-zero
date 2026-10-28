# Volume curve — where do the statistics stop moving?

**Status: MEASURED on the regenerated corpus. Not provisional.**
Repo commit `807d721a71ce754f633e6079a436c81ab389f4f9` (`807d721`).
Instrument: `measure.py` (lane C). Corpus: `corpus/` (lane B, regenerated).
Raw output retained: `curve-out-v2/` (this run) and `curve-out/` (superseded
pre-regeneration run, kept only so the diff below is reproducible).

## Corpus verified settled before measuring

The earlier version of this document was measured against a thin-fragment-bank
corpus and was labelled provisional for that reason. **That confound is gone.**
Verified by me on the current corpus before re-measuring, not taken on report:

| check | persona A | persona B |
|---|---|---|
| threads | 200 (manifest 200) OK | 200 (manifest 200) OK |
| outbound messages | 306 (manifest 306) OK | 291 (manifest 291) OK |
| sentence instances / unique | 2,091 / 1,293 | 4,494 / 3,020 |
| most-repeated sentence | 59x (19.3% of msgs) | 53x (18.2% of msgs) |
| messages repeating a sentence internally | 2.9% | 3.1% |
| JSON parse errors across 400 files | 0 | 0 |

Compare the pre-regeneration state: persona B had 862 unique sentences with its
top sentence appearing **241 times** and **86.7%** of messages repeating a
sentence internally. Unique material more than tripled and the repetition
collapsed. Counts were checked against `corpus/MANIFEST.json` and agree exactly,
and the newest corpus write was 107s old at measurement time with no writer
active — so this is not a torn read.

(My internal-duplication figure uses a different sentence-splitting and length
rule than the controller's, which reports 0.0%. The two rules disagree on
absolute value and agree on direction and magnitude. This document uses my rule
consistently throughout.)

## The standing limit — unchanged, and not a caveat that expires

This measures a **statistical harness** on **synthetic data with known ground
truth**. It bounds the MECHANICAL question only: how many messages until these
statistics settle. It says **nothing** about the human question of when a voice
is captured, and **this corpus must never be cited as evidence of human voice
capture**. Synthetic prose carries the generating model's fingerprints, so a
"did we capture the voice" pass over it measures model-recognising-model.

## Stabilisation definition (stated before measuring)

> An axis is **stable at limit N** when its value at N is within its own
> PERSONAS.md tolerance of its value at N=200, **and stays within that tolerance
> for every larger N measured.** The reported figure is the smallest such N.

Tolerance is a stated fraction of the axis's declared band width. Axis 8
(exclamations) is excluded — PERSONAS.md records its bands as overlapping, so it
is not in the battery. Axes 1a/1b (p25/p75) are reported by the instrument but
not counted.

**N=200 is a genuine subsample, not "the whole corpus."** With 306 and 291
outbound messages, limit=200 draws 200 of ~300. It does not alias to "all",
which is what makes it a meaningful reference point rather than a tautology.

## The curve — every axis at every limit

### Persona A (terse, no sign-off) — 306 outbound available

| axis | N=10 | N=25 | N=50 | N=75 | N=100 | N=200 | band w |
|---|---|---|---|---|---|---|---|
| median sentence len (w) | 6 | 6 | 6 | 6 | 6 | 6 | 4 |
| mean sentences/msg | 2.9 | 2.52 | 2.52 | 2.52 | 2.56 | 2.54 | 2 |
| mean paragraphs/msg | 1.3 | 1.12 | 1.14 | 1.12 | 1.12 | 1.105 | 0.4 |
| greeting % | 0 | 8 | 12 | 13.33 | 16 | 17.5 | 20 |
| sign-off % | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| hedging /100w | 0.5051 | 0.4444 | 0.2232 | 0.2946 | 0.2206 | 0.1372 | 0.6 |
| contractions /100w | 2.525 | 2.444 | 2.12 | 2.43 | 2.537 | 2.443 | 2 |
| formal /100w | 0 | 0 | 0 | 0.0736 | 0.1103 | 0.3568 | 0.8 |
| informal /100w | 2.02 | 1.556 | 2.567 | 2.651 | 2.482 | 2.69 | 1.6 |

### Persona B (verbose, formal) — 291 outbound available

| axis | N=10 | N=25 | N=50 | N=75 | N=100 | N=200 | band w |
|---|---|---|---|---|---|---|---|
| median sentence len (w) | 23 | 23 | 23 | 23 | 23 | 23 | 8 |
| mean sentences/msg | 8.9 | 9.12 | 9 | 8.947 | 8.86 | 8.935 | 4 |
| mean paragraphs/msg | 3.6 | 3.72 | 3.66 | 3.627 | 3.72 | 3.78 | 1.2 |
| greeting % | 100 | 100 | 100 | 100 | 100 | 100 | 0 |
| sign-off % | 100 | 100 | 100 | 100 | 100 | 100 | 0 |
| hedging /100w | 3.151 | 3.202 | 3.22 | 3.25 | 3.205 | 3.096 | 1.6 |
| contractions /100w | 0 | 0 | 0 | 0 | 0 | 0 | 0.4 |
| formal /100w | 4.078 | 4.658 | 4.588 | 4.507 | 4.561 | 4.817 | 2 |
| informal /100w | 0 | 0 | 0 | 0 | 0 | 0 | 0.2 |

Five persona B axes (greeting, sign-off, contractions, informal, median sentence
length) are **constant across the entire range**. That is the signature of a
categorical trait the generator applies to every message, not a statistic that
converges — they cannot contribute information to a stabilisation curve, and
they inflate any "how many axes are stable" count at every N including N=10.

## Discrimination curve

| limit | 10 | 25 | 50 | 75 | 100 | 200 |
|---|---|---|---|---|---|---|
| axes separated (k/9) | **9/9** | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 |
| in-band checks (corpus QA) | 17/18 | 17/18 | 17/18 | 18/18 | 18/18 | 18/18 |

**Headline: the smallest limit at which discrimination reaches its full-volume
value is 10 — the smallest limit measured.** The true value is "10 or fewer";
this instrument cannot resolve it further without measuring below 10. The curve
is saturated at its first sample point and therefore has no shape to report.

This is expected and is not a defect. The two personas are deliberately opposed
(bare-name vs. `Dear`, 0% vs. 100% sign-off, 6-word vs. 23-word sentences), so
ten messages separate them trivially. It confirms sound use #1 — the profiler
does not blend two opposed personas into mush — and says nothing about volume.

### IN-BAND misses are corpus QA, never discrimination failures

Three single-axis misses, one per small limit, each owned by lanes A/B:

- N=10: A greeting_pct = 0.0, band `[5, 25]` — at 10 messages an ~15% greeting
  rate can round to zero draws.
- N=25: A informal_per_100w = 1.5556, band `[2.0, 3.6]`.
- N=50: A contractions_per_100w = 2.1205, band `[2.2, 4.2]` — under by 0.08.

All three are small-sample sampling noise on persona A, and all clear by N=75.
Discrimination is 9/9 at every one of these limits.

**Predicted and confirmed:** persona B's p75 sentence length was flagged as the
tightest margin in the table. It lands **OUT of band at N=10 and N=25** (26.0
against band `[27, 37]`) and is in band from N=50 on, finishing at 27.0 — one
word above the floor at N=200. It is the most fragile axis measured. It is
reported by the instrument but not counted in the battery, so it does not appear
in the 17/18 figures above. **Corpus QA, not a discrimination failure.**

## Sensitivity analysis — THE HEADLINE

The stabilisation answer is governed by the tolerance you choose, not by a
threshold in the data. Same definition, three tolerances:

| tolerance | N=10 | N=25 | N=50 | N=75 | N=100 | N=200 |
|---|---|---|---|---|---|---|
| full band width | **18/18** | 18/18 | 18/18 | 18/18 | 18/18 | 18/18 |
| half band width | 15/18 | 16/18 | **18/18** | 18/18 | 18/18 | 18/18 |
| quarter band width | 12/18 | 14/18 | 15/18 | 16/18 | 17/18 | **18/18** |

**The spread SURVIVES the corpus fix.** The stabilisation point still moves
**10 -> 50 -> 200** purely by tightening tolerance. Bank exhaustion was not what
produced the effect: on a corpus with triple the unique material and no internal
repetition, the same three-way spread appears.

What *did* change is the middle row: half-band stabilisation moved **100 -> 50**,
i.e. the fixed corpus settles *earlier* under moderate tolerance, while the
quarter-band answer stayed pinned at 200. So the fix tightened the middle of the
range without collapsing the spread.

**There is no volume threshold in this data independent of how much drift you
are willing to call "stable."** Any single number quoted from this document
without its tolerance is noise.

Laggards under quarter-band tolerance, all persona A: `formal_per_100w` (never
stabilises before 200 — it climbs monotonically 0 -> 0 -> 0 -> 0.074 -> 0.110 ->
0.357 and is still rising at the top of the range), `hedging_per_100w` (100),
`greeting_pct` (75), `informal_per_100w` (50). Persona A carries every laggard;
persona B has none, because most of its axes are the constant categorical traits
noted above.

## What moved between the two corpora (v1 -> v2 at N=200)

Diffed against the retained `curve-out/` JSON. Two axes moved materially,
defined as >25% of band width:

| axis | v1 | v2 | delta | % of band |
|---|---|---|---|---|
| **B hedging /100w** | 4.113 | 3.096 | -1.017 | **63.6% MATERIAL** |
| **A contractions /100w** | 3.126 | 2.443 | -0.683 | **34.2% MATERIAL** |

Both moves are corrections in the expected direction. B's hedging was pinned near
its band ceiling by a handful of hedge-heavy sentences repeated hundreds of times
and has dropped to mid-band. A's contractions — the axis lane B flagged as
landing low, and the non-monotonic laggard in the previous run (3.478 -> 1.78 ->
3.126) — is now flat across the whole range (2.525 -> 2.12 -> 2.443) and sits
comfortably in band. **The previous run's headline laggard was a bank artifact
and is gone.**

Everything else moved by less than 25% of its band. Four axes are byte-identical
(B greeting, B sign-off, B contractions, B informal, A sign-off) — the constant
categorical traits. Notable smaller moves: A median sentence length 5 -> 6 and B
median 24 -> 23 (one word each, 25% and 12.5% of band).

## On the "75 emails per author" figure

Unchanged: **UNVERIFIED.** No paper reports it. The nearby real findings are that
50-100 samples gives a "convincing imitation" and that one study used 90 as an
experimental size; neither is a parity threshold, and neither is what this
measured.

This document measured a mechanical settling point on synthetic data with known
ground truth, under a stated tolerance, and found that the answer ranges from 10
to 200 depending on that tolerance alone. **It does not support, refute, or
replace the 75 figure**, and must not be cited as doing so.

## Reproduction

```
cd ~/scratch/synthetic-corpus
CONN=<path-to>/inbox-zero/connector
for L in 10 25 50 75 100 200; do
  for P in A B; do
    uv run --project $CONN python measure.py \
      --corpus corpus --persona $P --limit $L --json > curve-out-v2/p${P}-L${L}.json
  done
  uv run --project $CONN python measure.py \
    --corpus corpus --discriminate --limit $L > curve-out-v2/disc-L${L}.txt
done
```

All 18 invocations exited 0, all stderr empty. Note `uv run python -c ...`, never
`uv run -c ...`: `-c` is not a `uv run` flag, and passing it yields a uv USAGE
error on every file, which reads as a catastrophic corpus defect. Universal
failure across every input is a signal to suspect the instrument first.
