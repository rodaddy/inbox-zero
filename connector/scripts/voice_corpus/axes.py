"""The measurement core: stripping, detection, segmentation, and the tally.

Determinism-first (Rico, 2026-08-11): this is a script, not a model. Same
input, same numbers, always. It contains no judgment and no model call.

THE BATTERY IS NINE MEASURES ACROSS EIGHT AXES: 1, 2, 3, 4, 5, 6, 7, 9a, 9b.
Axis 8 (exclamation marks) OVERLAPS between the two personas on [0.0, 0.4]
(PERSONAS.md "Disjointness"). It is measured and reported but is NOT counted
toward separation: a discrimination test relying on exclamation rate is
relying on noise.

UNSOUND USE - read before citing anything this module computes. Synthetic
prose carries the generating model's fingerprints. These numbers must NEVER
be cited as evidence of human voice capture. They are sound for exactly four
things: falsification, mechanics, greeting mirroring, and the volume curve.

AMBIGUITY NOTES (PERSONAS.md wording -> reading chosen here)
-----------------------------------------------------------
A1. "Greeting and sign-off lines are EXCLUDED from word counts, sentence
    counts, and every per-100-word rate" (PERSONAS.md), but
    ``expected_stripped_body`` removes only the signature block and the
    quoted reply - the greeting and sign-off survive it. READING CHOSEN:
    start from ``expected_stripped_body``, then additionally drop a leading
    greeting line matching the closed allowed forms and a trailing sign-off
    block, and compute axes 1, 2, 3, 6, 7, 8, 9a, 9b on what remains. Axes 4
    and 5 are measured on the PRE-exclusion text, since they are exactly the
    presence of those lines.
A2. Sentence segmentation is not defined anywhere in PERSONAS.md. Dana uses
    lowercase sentence starts and fragments, so a capital-letter heuristic
    would be wrong for her. READING CHOSEN: split on a run of one or more
    terminal punctuation marks [.!?] followed by whitespace or end-of-text;
    a trailing fragment with no terminator still counts as one sentence.
A3. "Mean paragraphs per message" is not defined. READING CHOSEN: paragraphs
    are blank-line-separated blocks containing at least one non-whitespace
    character, counted on the greeting/sign-off-excluded body.
A4. Axis 8's denominator is "per 100 sentences" while every other rate is per
    100 words. Implemented literally as written.
A5. The closed lists contain both "ok" and "okay", and "rather"/"fairly".
    READING CHOSEN: every entry is matched independently on whole-word
    boundaries, so "okay" does NOT also fire the "ok" entry, and each
    occurrence counts once per matching entry.
A6. Multi-word entries ("it seems", "heads up", "in order to") are matched as
    contiguous phrases with a single run of whitespace between words, per
    PERSONAS.md "Multi-word entries are matched as contiguous phrases."
A7. Closed-list entries are matched case-insensitively as PERSONAS.md directs,
    so a lowercase "i believe" also counts.

Key Components:
    - strip_signature_and_quote: the fallback stripper for messages lacking
      ``expected_stripped_body`` (the ground truth is preferred when present).
    - detect_greeting / detect_signoff: the closed-form recognisers axes 4
      and 5 are built on.
    - exclude_greeting_and_signoff: note A1's exclusion, applied before every
      rate axis.
    - load_outbound: corpus loading, model-validated at the boundary, with a
      zero-messages refusal (absence of data must never read as a result).
    - measure: the per-persona pass; returns a typed ``AxisResult``.

Pattern/Convention:
    Everything here is pure functions plus one accumulator model; rendering
    and verdicts live in ``measure``. A new axis is a ``_Tally`` field, a
    fold line in ``_Tally.add``, and an ``AxisResult`` field - the closed
    lists and bands it reads come from ``config``, never from a local copy.

Example:
    >>> from voice_corpus.axes import split_sentences
    >>> split_sentences("yeah that works. I'll check.")
    ['yeah that works', "I'll check"]

See Also:
    - voice_corpus.config: the closed lists and bands this module measures by.
    - voice_corpus.measure: discrimination, rendering, and the CLI.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inbox_zero_connector.utils import logged
from voice_corpus.config import (
    CONTRACTIONS,
    FORMAL_MARKERS,
    HEDGING_MARKERS,
    INFORMAL_MARKERS,
    GreetingForm,
    Persona,
)
from voice_corpus.models import AxisResult, Message, Thread

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# --------------------------------------------------------------------------
# Greeting and sign-off recognition - PERSONAS.md "Greeting forms" and
# "Sign-off blocks - the exact allowed sets". Nothing outside them is
# recognised.
# --------------------------------------------------------------------------

# Dana: bare `<Name>,`. Margaret: `Dear <Name>,` or `Hi <Name>,`. The inbound
# side additionally uses `Hey <Name>,`; `Hello` is recognised so this harness
# can be pointed at inbound text without silently miscounting.
_GREETING_RE = re.compile(r"^(?:(Dear|Hi|Hey|Hello)\s+)?([A-Z][A-Za-z'-]*)\s*,\s*$")

_SIGNOFF_OPENERS = ("best regards,", "kind regards,", "with appreciation,")

_ATTRIBUTION_RE = re.compile(r"^On .* wrote:$")
_SIG_DELIM = "-- "

# Sentence terminator run followed by whitespace or end-of-text (note A2).
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")

_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def _phrase_pattern(entry: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive matcher for one closed-list entry.

    Multi-word entries match as contiguous phrases with a single run of
    whitespace between words (note A6). Word boundaries use an explicit
    lookaround rather than ``\\b`` so entries ending in an apostrophe-t
    ("don't") and entries that are prefixes of other entries ("ok" vs
    "okay") behave correctly (note A5).
    """
    body = r"\s+".join(re.escape(w) for w in entry.split())
    return re.compile(rf"(?<![A-Za-z0-9']){body}(?![A-Za-z0-9'])", re.IGNORECASE)


_HEDGING_PATTERNS = tuple(_phrase_pattern(e) for e in HEDGING_MARKERS)
_CONTRACTION_PATTERNS = tuple(_phrase_pattern(e) for e in CONTRACTIONS)
_FORMAL_PATTERNS = tuple(_phrase_pattern(e) for e in FORMAL_MARKERS)
_INFORMAL_PATTERNS = tuple(_phrase_pattern(e) for e in INFORMAL_MARKERS)


# --------------------------------------------------------------------------
# Text preparation
# --------------------------------------------------------------------------


def _is_attribution(line: str) -> bool:
    """True for the quoted-reply anchor line, ``On ... wrote:``."""
    return _ATTRIBUTION_RE.match(line.rstrip("\r")) is not None


def _is_sig_delimiter(line: str) -> bool:
    """True for the RFC 3676 ``-- `` delimiter (trailing-space tolerant)."""
    return line.rstrip("\r") == _SIG_DELIM or line.rstrip() == "--"


def _truncate_at(lines: list[str], is_cut: Callable[[str], bool]) -> list[str]:
    """Cut at the first line ``is_cut`` accepts, dropping that line,
    everything after it, and the blank line(s) immediately preceding it.
    No match returns the lines unchanged.
    """
    for idx, line in enumerate(lines):
        if is_cut(line):
            kept = lines[:idx]
            while kept and not kept[-1].strip():
                kept.pop()
            return kept
    return lines


def strip_signature_and_quote(body: str) -> str:
    """Fallback stripper for messages lacking ``expected_stripped_body``.

    PERSONAS.md "MECHANICS FIXTURES": remove the attribution line matching
    ``^On .* wrote:$``, every subsequent line, and the blank line immediately
    preceding it; and remove the ``-- `` delimiter line, everything after it,
    and the blank line immediately preceding that.

    This lane is NOT the stripping lane. Where the corpus supplies
    ``expected_stripped_body`` that value is used instead, because it is the
    known-correct answer.
    """
    lines = _truncate_at(body.split("\n"), _is_attribution)
    lines = _truncate_at(lines, _is_sig_delimiter)
    return "\n".join(lines).strip("\n")


def detect_greeting(body: str) -> GreetingForm | None:
    """Return the greeting FORM of the first line, or None.

    Forms returned: BARE_NAME, HI, HEY, HELLO, DEAR - the exact allowed sets
    from PERSONAS.md "Greeting forms".
    """
    lines = body.split("\n")
    if not lines:
        return None
    match = _GREETING_RE.match(lines[0].strip())
    if match is None:
        return None
    prefix = match.group(1)
    return GreetingForm.BARE_NAME if prefix is None else GreetingForm(prefix.lower())


def detect_signoff(body: str) -> str | None:
    """Return the sign-off FORM (lowercase, no comma), or None.

    PERSONAS.md allows exactly three closing blocks, each a closing line
    followed by the persona's full name on its own line. Dana's allowed set
    is THE EMPTY SET, so any Dana hit here is a generation failure to report,
    not a measurement error to absorb.
    """
    lines = [ln.strip() for ln in body.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    # The closing line is the last non-empty line, or the one above the name.
    for candidate in lines[-3:]:
        low = candidate.lower()
        if low in _SIGNOFF_OPENERS:
            return low.rstrip(",")
    return None


def exclude_greeting_and_signoff(body: str) -> str:
    """Drop the greeting line and the sign-off block (note A1).

    PERSONAS.md: "Greeting and sign-off lines are EXCLUDED from word counts,
    sentence counts, and every per-100-word rate - otherwise Persona B's
    fixed sign-off block inflates her formality rate mechanically and the
    axis stops measuring prose."
    """
    lines = body.split("\n")

    if lines and _GREETING_RE.match(lines[0].strip()):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)

    cuts = [idx for idx, line in enumerate(lines) if line.strip().lower() in _SIGNOFF_OPENERS]
    if cuts:
        lines = lines[: cuts[0]]

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Per-message primitives
# --------------------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    """Segment into sentences (note A2 - no capital-letter heuristic)."""
    flat = " ".join(ln.strip() for ln in text.split("\n") if ln.strip())
    if not flat:
        return []
    return [p.strip() for p in _SENTENCE_SPLIT_RE.split(flat) if p.strip()]


def count_words(text: str) -> int:
    """Words are apostrophe-tolerant alphanumeric runs (one rule, one place)."""
    return len(_WORD_RE.findall(text))


def count_paragraphs(text: str) -> int:
    """Blank-line-separated blocks with at least one non-whitespace char (A3)."""
    return sum(1 for b in re.split(r"\n\s*\n", text) if b.strip())


def count_markers(text: str, patterns: tuple[re.Pattern[str], ...]) -> int:
    """Total occurrences of every closed-list entry, each counted per note A5."""
    return sum(len(p.findall(text)) for p in patterns)


# --------------------------------------------------------------------------
# Corpus loading
# --------------------------------------------------------------------------


@logged
def load_outbound(corpus_dir: Path, persona: Persona, limit: int | None) -> list[Message]:
    """Load a persona's OUTBOUND messages only, in thread order.

    Thread order is the sorted filename order (``thread-0001.json`` ...), and
    within a thread the message order as emitted. ``limit`` takes the first N
    outbound messages in exactly that order - the knob the volume curve turns.

    Every file is validated through the ``Thread`` model before any message
    is taken from it: a corpus whose shape has drifted fails HERE, at the
    boundary, rather than silently measuring fewer messages than the caller
    believes it measured.

    GUARD: an existing but EMPTY persona directory yields zero messages. Left
    alone that becomes a clean-looking zero downstream, so it is raised as a
    refusal instead - absence of data is never allowed to read as a result.

    Raises:
        FileNotFoundError: No such persona directory.
        ValueError: A file fails schema validation, or zero outbound messages
            were found.
    """
    sub = corpus_dir / f"persona-{persona.value.lower()}"
    if not sub.is_dir():
        raise FileNotFoundError(f"no such persona directory: {sub}")

    out: list[Message] = []
    for path in sorted(sub.glob("thread-*.json")):
        try:
            thread = Thread.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"{path}: does not match the corpus schema: {exc}") from exc
        outbound = [m for m in thread.messages if m.direction == "outbound"]
        out.extend(outbound)
        if limit is not None and len(out) >= limit:
            out = out[:limit]
            break

    if not out:
        raise ValueError(
            f"{sub}: zero outbound messages loaded -- an empty measurement is "
            f"not a result. Regenerate the corpus with generate.py."
        )
    logger.info("LOAD: persona={} messages={}", persona.value, len(out))
    return out


def body_for_measurement(msg: Message) -> str:
    """The stripped body - ground truth where the corpus supplies it."""
    expected = msg.ground_truth.expected_stripped_body
    return expected if expected is not None else strip_signature_and_quote(msg.body)


# --------------------------------------------------------------------------
# The nine axes
# --------------------------------------------------------------------------


class _Tally(BaseModel):
    """Mutable accumulator for one pass over a persona's outbound messages.

    A model rather than fifteen locals threaded through helper signatures:
    the accumulation and the ratio arithmetic are two genuinely different
    steps, and splitting them is what keeps ``measure`` inside the package's
    50-code-line function ceiling without changing a single number.
    """

    model_config = ConfigDict(extra="forbid")

    sentence_lengths: list[int] = Field(default_factory=list)
    sentences_per_message: list[int] = Field(default_factory=list)
    paragraphs_per_message: list[int] = Field(default_factory=list)
    total_words: int = 0
    total_sentences: int = 0
    hedging: int = 0
    contractions: int = 0
    formal: int = 0
    informal: int = 0
    exclamations: int = 0
    greeting_forms: Counter[str] = Field(default_factory=Counter)
    signoff_forms: Counter[str] = Field(default_factory=Counter)
    with_greeting: int = 0
    with_signoff: int = 0

    def add(self, msg: Message) -> None:
        """Fold one message into the running totals.

        Axes 4 and 5 read the PRE-exclusion text because they measure exactly
        the presence of the lines the rate axes exclude (note A1).
        """
        stripped = body_for_measurement(msg)

        greeting = detect_greeting(stripped)
        self.with_greeting += int(greeting is not None)
        self.greeting_forms["none" if greeting is None else greeting.value] += 1

        signoff = detect_signoff(stripped)
        self.with_signoff += int(signoff is not None)
        self.signoff_forms["none" if signoff is None else signoff] += 1

        prose = exclude_greeting_and_signoff(stripped)

        sents = split_sentences(prose)
        self.sentences_per_message.append(len(sents))
        self.total_sentences += len(sents)
        self.sentence_lengths.extend(count_words(sent) for sent in sents)

        self.paragraphs_per_message.append(count_paragraphs(prose))
        self.total_words += count_words(prose)

        self.exclamations += prose.count("!")
        self.hedging += count_markers(prose, _HEDGING_PATTERNS)
        self.contractions += count_markers(prose, _CONTRACTION_PATTERNS)
        self.formal += count_markers(prose, _FORMAL_PATTERNS)
        self.informal += count_markers(prose, _INFORMAL_PATTERNS)


@logged
def measure(messages: list[Message]) -> AxisResult:
    """Compute every axis over a set of outbound messages.

    GUARD: an empty message list is refused rather than measured. Zero
    messages would otherwise produce a full table of zeroes that looks like a
    finding, and a measurement over nothing must never be presentable as one.

    Raises:
        ValueError: ``messages`` is empty.
    """
    if not messages:
        raise ValueError("no outbound messages to measure")

    tally = _Tally()
    for msg in messages:
        tally.add(msg)
    logger.debug(
        "MEASURE: messages={} words={} sentences={}",
        len(messages),
        tally.total_words,
        tally.total_sentences,
    )
    return _ratios(tally, len(messages))


def _quantiles(sentence_lengths: list[int]) -> tuple[float, float, float]:
    """(median, p25, p75) with the empty-input zero fallback."""
    if not sentence_lengths:
        return 0.0, 0.0, 0.0
    ordered = sorted(sentence_lengths)
    quantiles = statistics.quantiles(ordered, n=4, method="inclusive")
    return float(statistics.median(ordered)), float(quantiles[0]), float(quantiles[2])


def _ratios(t: _Tally, n: int) -> AxisResult:
    """Turn accumulated counts into the reported per-100 rates and quantiles."""

    def per_100w(count: int) -> float:
        return round(count * 100.0 / t.total_words, 4) if t.total_words else 0.0

    def pct(count: int) -> float:
        return round(count * 100.0 / n, 4)

    def dist(forms: Counter[str]) -> dict[str, float]:
        return {k: round(v * 100.0 / n, 4) for k, v in sorted(forms.items())}

    median, p25, p75 = _quantiles(t.sentence_lengths)

    return AxisResult(
        messages_measured=n,
        total_words=t.total_words,
        total_sentences=t.total_sentences,
        median_sentence_len_words=round(median, 4),
        sentence_len_p25_words=round(p25, 4),
        sentence_len_p75_words=round(p75, 4),
        mean_sentences_per_message=round(statistics.fmean(t.sentences_per_message), 4),
        mean_paragraphs_per_message=round(statistics.fmean(t.paragraphs_per_message), 4),
        greeting_pct=pct(t.with_greeting),
        greeting_form_distribution_pct=dist(t.greeting_forms),
        signoff_pct=pct(t.with_signoff),
        signoff_form_distribution_pct=dist(t.signoff_forms),
        hedging_per_100w=per_100w(t.hedging),
        contractions_per_100w=per_100w(t.contractions),
        # Axis 8 - REPORTED, NOT IN THE BATTERY (bands overlap).
        exclamations_per_100s=(
            round(t.exclamations * 100.0 / t.total_sentences, 4) if t.total_sentences else 0.0
        ),
        formal_per_100w=per_100w(t.formal),
        informal_per_100w=per_100w(t.informal),
    )
