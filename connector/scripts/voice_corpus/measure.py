"""The falsification battery: discrimination, rendering, and the harness CLI.

Two independent questions, reported separately and never collapsed into one
number, because collapsing them is how a corpus-QA miss gets mistaken for a
falsification failure:

    IN-BAND   (Q1, corpus QA)     - is each measured value inside its OWN
                                    declared band? A miss is a finding
                                    against the GENERATOR.
    SEPARATED (Q2, falsification) - are the two personas DISTINGUISHABLE?
                                    This is the question the corpus exists
                                    to answer.

Axis definitions, the closed word lists, the tolerance bands, and the
measurement core live in ``voice_corpus.axes`` and ``voice_corpus.config``;
this module owns the verdict and its presentation.

UNSOUND USE - read before citing anything this script prints. Synthetic
prose carries the generating model's fingerprints. These numbers must NEVER
be cited as evidence of human voice capture. They are sound for exactly four
things: falsification, mechanics, greeting mirroring, and the volume curve.

LOGGING SCOPE - a stated judgment call, not an omission
    This is a CLI instrument that prints a table and exits, not a service.
    The ``logged`` decorator covers the work boundaries; there is no
    second process to correlate with and no daemon lifetime to rotate over.
    The rule that IS load-bearing is honoured end to end: a missing corpus,
    an unparseable corpus, or a persona directory holding zero outbound
    messages each exit 2 with the reason on stderr. This instrument never
    prints a clean ``0/9`` over nothing.

Key Components:
    - band_geometry / separated / conforms: the two questions, as arithmetic.
    - discriminate: the battery verdict as a typed ``Discrimination``.
    - print_axes / print_discrimination: the tables, driven by AXIS_ROWS
      rather than a wall of hand-aligned prints.
    - main: the CLI. Exit 0 printed a measurement; exit 2 refused. In
      ``--discriminate`` mode the exit code is 0 REGARDLESS of the verdict -
      a verdict is information, and making it an exit code invites someone
      to "fix" the corpus until a check goes green.

Pattern/Convention:
    Run from ``connector/scripts``::

        uv run python -m voice_corpus.measure --corpus voice_corpus/corpus --discriminate
        uv run python -m voice_corpus.measure --corpus voice_corpus/corpus --persona A --limit 50

    The blended control (both persona directories holding the SAME persona's
    threads) must report 0/9 - that is the RED proof that the battery can
    fail. Build it by copying ``persona-b`` over both names in a scratch dir;
    no code path fabricates it.

Example:
    >>> from voice_corpus.config import AxisKey
    >>> from voice_corpus.measure import band_geometry
    >>> band_geometry(AxisKey.MEDIAN_SENTENCE_LEN)
    (14.0, 12)

See Also:
    - voice_corpus.axes: the measurement core this consumes.
    - voice_corpus.config: BANDS, BATTERY, and the enums.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

from loguru import logger

from inbox_zero_connector.utils import logged
from voice_corpus.axes import load_outbound, measure
from voice_corpus.config import (
    BANDS,
    BATTERY,
    BATTERY_SIZE,
    NON_BATTERY,
    AxisKey,
    Persona,
    configure_logging,
)
from voice_corpus.models import AxisResult, BatteryRow, Discrimination

PERSONA_BAND_INDEX: Final[dict[Persona, int]] = {Persona.A: 0, Persona.B: 1}
"""Which side of a BANDS entry each persona reads. A table, not a branch."""

# Fraction of the declared band gap that the two measured values must span
# before the axis counts as separated. 0.5 means they must be at least half
# the declared gap apart - enough that a small calibration miss or sampling
# noise does not flip the verdict, while a genuine blend (both values landing
# near each other) still fails.
SEPARATION_MARGIN_FRACTION: Final = 0.5

SEPARATION_RULE: Final = (
    "SEPARATED = the two measured values lie on OPPOSITE SIDES of the midpoint "
    "between the two declared bands, AND are at least "
    f"{SEPARATION_MARGIN_FRACTION:g}x the declared band gap apart. "
    "This asks only whether the personas are distinguishable. It deliberately "
    "does NOT require either value to sit inside its own band -- that is the "
    "separate IN-BAND (corpus QA) count."
)


def band_geometry(key: AxisKey) -> tuple[float, float]:
    """Midpoint of the gap between the two declared bands, and the gap width.

    The bands may be ordered either way round (A below B on axis 1, A above B
    on axis 7), so the gap is computed between whichever band is lower. On
    overlap (axis 8) no gap exists and the width returned is 0.0.
    """
    (a_lo, a_hi), (b_lo, b_hi) = BANDS[key]
    if a_hi <= b_lo:  # A's band sits below B's
        return ((a_hi + b_lo) / 2.0, b_lo - a_hi)
    if b_hi <= a_lo:  # B's band sits below A's
        return ((b_hi + a_lo) / 2.0, a_lo - b_hi)
    # Bands overlap (axis 8) - no gap exists.
    return ((max(a_lo, b_lo) + min(a_hi, b_hi)) / 2.0, 0.0)


def separated(key: AxisKey, a_value: float, b_value: float) -> bool:
    """Q2 - are the two personas DISTINGUISHABLE? (the falsification question)

    Separation is measured against the DECLARED BAND GAP per SEPARATION_RULE;
    conformance is reported separately by ``conforms``. An earlier reading
    that required each value inside its OWN band conflated the two questions
    and understated discrimination at small ``--limit`` values, where
    sampling noise pushes a value outside a tight band.
    """
    midpoint, gap = band_geometry(key)
    if gap <= 0.0:
        # Bands overlap (axis 8). No declared gap exists, so no separation
        # can be claimed from this axis regardless of the values.
        return False
    opposite_sides = (a_value - midpoint) * (b_value - midpoint) < 0
    far_enough = abs(a_value - b_value) >= SEPARATION_MARGIN_FRACTION * gap
    return opposite_sides and far_enough


def conforms(key: AxisKey, value: float, persona: Persona) -> bool:
    """CONFORMANCE (Q1) - does this measured value sit inside its OWN band?

    Reported alongside separation but never counted into the k/9 line. A
    conformance miss is a FINDING against the generator or the axis targets;
    it is not evidence the personas fail to separate.
    """
    lo, hi = BANDS[key][PERSONA_BAND_INDEX[persona]]
    return lo <= value <= hi


def _row(
    axis: str,
    key: AxisKey,
    a: AxisResult,
    b: AxisResult,
    *,
    excluded_because: str | None = None,
) -> BatteryRow:
    """One verdict row - both values, both bands, both questions together.

    Battery membership IS the absence of an exclusion reason: every
    non-battery axis has one (config.NON_BATTERY), every battery axis has
    none, so a second boolean would only be a chance to disagree with it.
    """
    a_value, b_value = a.value(key), b.value(key)
    return BatteryRow(
        axis=axis,
        measure=key.value,
        a_value=a_value,
        a_band=list(BANDS[key][0]),
        a_conforms=conforms(key, a_value, Persona.A),
        b_value=b_value,
        b_band=list(BANDS[key][1]),
        b_conforms=conforms(key, b_value, Persona.B),
        separated=separated(key, a_value, b_value),
        in_battery=excluded_because is None,
        excluded_because=excluded_because,
    )


@logged
def discriminate(a: AxisResult, b: AxisResult) -> Discrimination:
    """Score both personas across the battery and the reported-only axes.

    ``separated_count`` counts battery axes only; ``in_band_count`` counts
    every band check in the battery (both personas x nine measures = 18).
    The non-battery rows are carried through so a reader can see axis 8's
    overlap rather than wonder why it vanished.
    """
    rows = [_row(axis, key, a, b) for axis, key in BATTERY]
    rows += [_row(axis, key, a, b, excluded_because=reason) for axis, key, reason in NON_BATTERY]

    battery_rows = [r for r in rows if r.in_battery]
    nonconforming = [
        miss
        for r in battery_rows
        for ok, miss in (
            (
                r.a_conforms,
                f"A axis {r.axis} ({r.measure})={r.a_value} outside {BANDS[AxisKey(r.measure)][0]}",
            ),
            (
                r.b_conforms,
                f"B axis {r.axis} ({r.measure})={r.b_value} outside {BANDS[AxisKey(r.measure)][1]}",
            ),
        )
        if not ok
    ]
    return Discrimination(
        rows=rows,
        separated_count=sum(r.separated for r in battery_rows),
        battery_size=BATTERY_SIZE,
        in_band_count=sum(r.a_conforms + r.b_conforms for r in battery_rows),
        in_band_total=BATTERY_SIZE * 2,
        nonconforming=nonconforming,
        separation_rule=SEPARATION_RULE,
    )


# --------------------------------------------------------------------------
# Rendering. The axis table is DATA (label rows), not a wall of prints.
# --------------------------------------------------------------------------

AXIS_ROWS: Final[tuple[tuple[str, str, AxisKey, str], ...]] = (
    ("1", "median sentence length (words)", AxisKey.MEDIAN_SENTENCE_LEN, ""),
    ("1a", "sentence length p25 (words)", AxisKey.SENTENCE_LEN_P25, ""),
    ("1b", "sentence length p75 (words)", AxisKey.SENTENCE_LEN_P75, ""),
    ("2", "mean sentences per message", AxisKey.MEAN_SENTENCES, ""),
    ("3", "mean paragraphs per message", AxisKey.MEAN_PARAGRAPHS, ""),
    ("4", "messages with a greeting (%)", AxisKey.GREETING_PCT, ""),
    ("5", "messages with a sign-off (%)", AxisKey.SIGNOFF_PCT, ""),
    ("6", "hedging markers /100 words", AxisKey.HEDGING, ""),
    ("7", "contractions /100 words", AxisKey.CONTRACTIONS, ""),
    ("8", "exclamations /100 sentences", AxisKey.EXCLAMATIONS, "  NOT IN BATTERY (bands overlap)"),
    ("9a", "formal markers /100 words", AxisKey.FORMAL, ""),
    ("9b", "informal markers /100 words", AxisKey.INFORMAL, ""),
)

FORM_DISTRIBUTIONS: Final[dict[AxisKey, str]] = {
    AxisKey.GREETING_PCT: "greeting_form_distribution_pct",
    AxisKey.SIGNOFF_PCT: "signoff_form_distribution_pct",
}
"""Axes whose form-distribution sub-line prints beneath their value row."""


def print_axes(persona: Persona, m: AxisResult) -> None:
    """Print one persona's axis table with an in/OUT mark against its band."""
    band_idx = PERSONA_BAND_INDEX[persona]

    print(
        f"PERSONA {persona.value} -- {m.messages_measured} outbound messages, "
        f"{m.total_words} words, {m.total_sentences} sentences"
    )
    for axis, label, key, note in AXIS_ROWS:
        lo, hi = BANDS[key][band_idx]
        value = m.value(key)
        mark = "in " if lo <= value <= hi else "OUT"
        print(f"  {axis:<3}{label:<31}: {value:>8}  [{lo}, {hi}] {mark}{note}")
        forms_field = FORM_DISTRIBUTIONS.get(key)
        if forms_field is not None:
            print(f"       forms: {getattr(m, forms_field)}")


def print_discrimination(verdict: Discrimination, limit_label: str) -> None:
    """Print the battery verdict, both counts, and the rule that made them.

    The separation RULE prints alongside the number every time. A bare "9/9"
    invites someone to tune the rule until it reads nine.
    """
    print("BATTERY = axes 1, 2, 3, 4, 5, 6, 7, 9a, 9b -- nine disjoint measures across eight axes.")
    print(
        "Axis 8 is measured and reported but EXCLUDED: its bands overlap on "
        "[0.0, 0.4] (PERSONAS.md)."
    )
    print()
    print("TWO INDEPENDENT QUESTIONS, reported separately and never collapsed:")
    print(
        "  IN-BAND   (Q1, corpus QA)      -- is each measured value inside its OWN declared band?"
    )
    print("  SEPARATED (Q2, falsification)  -- " + SEPARATION_RULE.split(". ")[0] + ".")
    print("    " + ". ".join(SEPARATION_RULE.split(". ")[1:]).strip())
    print()
    print(
        f"{'axis':<5}{'measure':<30}{'A value':>10}{'A band':>14}{'A?':>5}"
        f"{'B value':>10}{'B band':>14}{'B?':>5}  verdict"
    )
    for row in verdict.rows:
        a_band = f"[{row.a_band[0]}, {row.a_band[1]}]"
        b_band = f"[{row.b_band[0]}, {row.b_band[1]}]"
        a_ok = "in" if row.a_conforms else "OUT"
        b_ok = "in" if row.b_conforms else "OUT"
        battery_mark = "SEPARATED" if row.separated else "not separated"
        mark = battery_mark if row.in_battery else "reported, not counted"
        print(
            f"{row.axis:<5}{row.measure:<30}{row.a_value:>10}{a_band:>14}"
            f"{a_ok:>5}{row.b_value:>10}{b_band:>14}{b_ok:>5}  {mark}"
        )
    print()
    if verdict.nonconforming:
        print(
            "IN-BAND misses (corpus QA -- lane B/lane A own these, "
            "they do NOT reduce the discrimination count):"
        )
        for miss in verdict.nonconforming:
            print(f"  - {miss}")
        print()
    print(
        f"IN-BAND: {verdict.in_band_count}/{verdict.in_band_total} "
        f"band checks inside their own band at limit={limit_label}"
    )
    print(
        f"DISCRIMINATION: {verdict.separated_count}/{verdict.battery_size} "
        f"axes separated at limit={limit_label}"
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse and validate the CLI. Split out so ``main`` stays inside the
    package's 50-code-line function ceiling."""
    parser = argparse.ArgumentParser(
        description="Measure the nine axes of the synthetic corpus. "
        "An instrument, not a gate -- it never fails on a verdict."
    )
    parser.add_argument(
        "--corpus", required=True, type=Path, help="path to the generated corpus/ directory"
    )
    parser.add_argument("--persona", choices=["A", "B", "a", "b"], help="measure one persona")
    parser.add_argument(
        "--discriminate",
        action="store_true",
        help="measure both personas and print the battery verdict",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="use only the first N outbound messages, thread order",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of the table"
    )
    args = parser.parse_args(argv)

    if args.discriminate == bool(args.persona):
        parser.error("give exactly one of --persona or --discriminate")
    return args


def _run_discriminate(args: argparse.Namespace, limit_label: str) -> int:
    """Measure both personas and print the battery verdict.

    Exit 0 ALWAYS in this mode. A verdict is INFORMATION; making it an exit
    code invites someone to "fix" the corpus until the check goes green,
    which would destroy the only thing the corpus is for.
    """
    a = measure(load_outbound(args.corpus, Persona.A, args.limit))
    b = measure(load_outbound(args.corpus, Persona.B, args.limit))
    verdict = discriminate(a, b)
    if args.as_json:
        print(
            json.dumps(
                {
                    "limit": args.limit,
                    "persona_a": a.model_dump(),
                    "persona_b": b.model_dump(),
                    "discrimination": verdict.model_dump(exclude_none=True),
                },
                indent=2,
            )
        )
    else:
        print_axes(Persona.A, a)
        print()
        print_axes(Persona.B, b)
        print()
        print_discrimination(verdict, limit_label)
    logger.info(
        "DISCRIMINATE: separated={}/{} in_band={}/{} limit={}",
        verdict.separated_count,
        verdict.battery_size,
        verdict.in_band_count,
        verdict.in_band_total,
        limit_label,
    )
    return 0


def _run_persona(args: argparse.Namespace) -> int:
    """Measure a single persona and print its axis table."""
    persona = Persona(str(args.persona).upper())
    m = measure(load_outbound(args.corpus, persona, args.limit))
    if args.as_json:
        print(
            json.dumps(
                {"persona": persona.value, "limit": args.limit, "axes": m.model_dump()},
                indent=2,
            )
        )
    else:
        print_axes(persona, m)
    logger.info("PERSONA: persona={} messages={}", persona.value, m.messages_measured)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the harness. Exit 0 on a completed measurement, 2 on a refusal.

    A refusal is a missing corpus directory, a corpus whose shape does not
    validate, or a persona with zero outbound messages. Each says why on
    stderr. This instrument never reports an empty measurement as a result.
    """
    args = _parse_args(argv)
    configure_logging()
    logger.debug(
        "measure entry corpus={} persona={} discriminate={} limit={}",
        args.corpus,
        args.persona,
        args.discriminate,
        args.limit,
    )

    limit_label = str(args.limit) if args.limit is not None else "all"

    try:
        if args.discriminate:
            return _run_discriminate(args, limit_label)
        return _run_persona(args)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        # The @logged decorators recorded the traceback; this boundary turns
        # it into the operator-facing refusal and the exit-2 contract.
        logger.error("measure REFUSED: {}", exc)
        print(f"measure.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
