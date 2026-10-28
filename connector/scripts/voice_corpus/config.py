"""ALL configuration for the voice-corpus instruments, in one place.

WHY EVERYTHING IS HERE AND NOTHING IS HARDCODED IN THE MODULES
    ``generate`` and ``measure`` must agree on the closed word lists, the
    tolerance bands, and the seed, or the harness measures something the
    generator never aimed at. While each module carried its own copy of those
    facts they agreed by convention; here they agree by construction - one
    definition, both import it. This follows the keystone pattern the
    connector already uses (``src/inbox_zero_connector/config.py``): typed,
    frozen configuration that application modules read rather than embed.
    The keystone also owns logging setup, so ``configure_logging`` lives here
    and delegates to the connector's own engine rather than building a second
    one.

WHAT IS DELIBERATELY *NOT* SETTINGS
    The closed word lists and the tolerance bands are NOT tunable knobs, so
    they are module-level constants rather than settings fields with
    environment overrides. ``PERSONAS.md`` calls the lists CLOSED, and a band
    an environment variable can widen is a band that will be widened the
    first time a run goes red. ``CorpusPaths`` and ``GeneratorSettings`` ARE
    settings - where the corpus lives and how much of it to build are
    legitimately per-run.

Key Components:
    - Persona / GreetingForm / SignoffForm / AxisKey: the closed StrEnum sets
      every dispatch table in the package is keyed by.
    - CorpusPaths / GeneratorSettings: the run knobs, frozen pydantic models.
    - PersonaSpec / PERSONAS: the two fixed personas as typed fixtures.
    - HEDGING_MARKERS / CONTRACTIONS / FORMAL_MARKERS / INFORMAL_MARKERS: the
      four CLOSED word lists (axes 6, 7, 9a, 9b).
    - BANDS / BATTERY / NON_BATTERY: the nine-axis table and its exclusions.
    - INBOUND_GREETING_FORMS / MARGARET_MIRROR: the issue-#83 ground truth.
    - configure_logging: the one logging call, delegating to the connector.

Pattern/Convention:
    Import the name you need; never re-declare one of these values locally -
    a second copy of a band is a silent disagreement waiting to happen. The
    numeric literals in ``BANDS`` keep their original int/float spelling on
    purpose: the harness prints them verbatim, and the printed receipt is
    part of what byte-stability is measured against.

Example:
    >>> from voice_corpus.config import BANDS, AxisKey
    >>> BANDS[AxisKey.GREETING_PCT]
    ((5, 25), (100, 100))

See Also:
    - PERSONAS.md: the human-readable spec these values implement.
    - src/inbox_zero_connector/config.py: the keystone-config convention.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from inbox_zero_connector.config import LoggingSettings
from inbox_zero_connector.utils.logging_config import setup as _engine_setup

PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
"""This package's own directory. Resolved from ``__file__`` so the spec and
the corpus are found relative to the CODE, never to the caller's cwd - the
relative-path coupling that stranded this tooling in a temp tree once."""

LOG_ENV_TAG: Final = "voice-corpus"
"""Distinguishes these runs from the connector's own in any shared sink."""

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def configure_logging(level: LogLevel = "INFO") -> None:
    """Point loguru at stderr only, at ``level``, using the connector engine.

    File and JSON sinks are OFF: these are one-shot instruments run by hand,
    and a rotating file in the user data dir per invocation would be litter
    nobody reads. ``LoggingSettings`` is fully defaulted, so it constructs
    standalone without loading connector runtime state - verified, which is
    what makes this reuse a boundary and not a coupling.

    Args:
        level: Minimum level for the stderr sink.
    """
    _engine_setup(
        LoggingSettings(level=level, file_sink=False, json_sink=False),
        env=LOG_ENV_TAG,
    )


class Persona(StrEnum):
    """The two fixed personas. The value is the corpus's persona key."""

    A = "A"
    B = "B"


class GreetingForm(StrEnum):
    """The closed greeting forms (PERSONAS.md "Greeting forms").

    ``HELLO`` appears on no generation table - it exists so the harness can
    be pointed at inbound text without silently miscounting a form the
    generator never emits.
    """

    BARE_NAME = "bare_name"
    HEY = "hey"
    HI = "hi"
    DEAR = "dear"
    HELLO = "hello"
    NONE = "none"


class SignoffForm(StrEnum):
    """Margaret's three allowed closing lines. Dana's allowed set is empty."""

    BEST_REGARDS = "Best regards,"
    KIND_REGARDS = "Kind regards,"
    WITH_APPRECIATION = "With appreciation,"


class AxisKey(StrEnum):
    """The measure names of the nine-axis table. Value = result field name."""

    MEDIAN_SENTENCE_LEN = "median_sentence_len_words"
    SENTENCE_LEN_P25 = "sentence_len_p25_words"
    SENTENCE_LEN_P75 = "sentence_len_p75_words"
    MEAN_SENTENCES = "mean_sentences_per_message"
    MEAN_PARAGRAPHS = "mean_paragraphs_per_message"
    GREETING_PCT = "greeting_pct"
    SIGNOFF_PCT = "signoff_pct"
    HEDGING = "hedging_per_100w"
    CONTRACTIONS = "contractions_per_100w"
    EXCLAMATIONS = "exclamations_per_100s"
    FORMAL = "formal_per_100w"
    INFORMAL = "informal_per_100w"


class CorpusPaths(BaseModel):
    """Where the spec and the generated corpus live.

    Both default INSIDE the package directory: the generator, the spec it
    implements, and the data it produces share one directory and one
    lifetime, so the corpus can never drift away from the code that made it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: Path = Field(default=PACKAGE_DIR / "PERSONAS.md")
    """The ground-truth spec. Read by humans; implemented by both modules."""

    corpus: Path = Field(default=PACKAGE_DIR / "corpus")
    """The generated corpus. GITIGNORED - regenerable from the seed."""


class GeneratorSettings(BaseModel):
    """The two run knobs. Everything else about the corpus is fixed by spec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int = 20260819
    """Same seed in, byte-identical corpus out. Changing it makes a DIFFERENT
    corpus - not a corrected one - and invalidates every recorded number."""

    threads_per_persona: int = Field(default=200, ge=1)
    """``ge=1`` is a guard, not decoration: zero threads would write an empty
    corpus while every count printed zero and the exit code said success."""


DEFAULT_SEED: Final[int] = GeneratorSettings().seed
THREADS_PER_PERSONA: Final[int] = GeneratorSettings().threads_per_persona

# --------------------------------------------------------------------------
# Closed word lists - PERSONAS.md "Closed word lists - axes 6, 7, 9".
# These lists are CLOSED. Do not add entries; a generator may not introduce a
# marker outside these lists and expect it to count.
# --------------------------------------------------------------------------

HEDGING_MARKERS: Final[tuple[str, ...]] = (
    "perhaps",
    "possibly",
    "it seems",
    "it would seem",
    "I would suggest",
    "I would think",
    "I believe",
    "I suspect",
    "somewhat",
    "rather",
    "fairly",
    "to some extent",
    "if I may",
    "I wonder whether",
    "it may be",
    "it might be",
    "arguably",
    "in my view",
    "I am inclined to",
    "one could argue",
)

CONTRACTIONS: Final[tuple[str, ...]] = (
    "don't",
    "doesn't",
    "didn't",
    "can't",
    "won't",
    "wouldn't",
    "shouldn't",
    "couldn't",
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
    "haven't",
    "hasn't",
    "hadn't",
    "it's",
    "that's",
    "there's",
    "we'll",
    "we're",
    "we've",
    "I'm",
    "I've",
    "I'll",
)

FORMAL_MARKERS: Final[tuple[str, ...]] = (
    "regarding",
    "concerning",
    "furthermore",
    "moreover",
    "nevertheless",
    "accordingly",
    "subsequently",
    "therefore",
    "with respect to",
    "in the event that",
    "at your earliest convenience",
    "please be advised",
    "kindly",
    "I trust",
    "should you require",
    "prior to",
    "in order to",
    "provide",
    "request",
    "correspondence",
)

INFORMAL_MARKERS: Final[tuple[str, ...]] = (
    "yeah",
    "yep",
    "nope",
    "ok",
    "okay",
    "thanks",
    "got it",
    "sure",
    "fine",
    "quick",
    "just",
    "stuff",
    "guys",
    "heads up",
    "FYI",
    "ping",
    "grab",
    "sort out",
    "figure out",
    "no worries",
)

# --------------------------------------------------------------------------
# Tolerance bands - PERSONAS.md "THE NINE-AXIS NUMERIC TABLE".
# key -> (A band, B band). Bands are inclusive [lo, hi]. The int/float
# spelling of each literal is deliberate: the harness prints bands verbatim.
# --------------------------------------------------------------------------

Band = tuple[float, float]

BANDS: Final[dict[AxisKey, tuple[Band, Band]]] = {
    AxisKey.MEDIAN_SENTENCE_LEN: ((4, 8), (20, 28)),
    AxisKey.SENTENCE_LEN_P25: ((2, 6), (13, 21)),
    AxisKey.SENTENCE_LEN_P75: ((6, 12), (27, 37)),
    AxisKey.MEAN_SENTENCES: ((1.5, 3.5), (7.0, 11.0)),
    AxisKey.MEAN_PARAGRAPHS: ((0.9, 1.3), (2.8, 4.0)),
    AxisKey.GREETING_PCT: ((5, 25), (100, 100)),
    AxisKey.SIGNOFF_PCT: ((0, 0), (100, 100)),
    AxisKey.HEDGING: ((0.0, 0.6), (2.7, 4.3)),
    AxisKey.CONTRACTIONS: ((2.2, 4.2), (0.0, 0.4)),
    AxisKey.EXCLAMATIONS: ((0.0, 2.0), (0.0, 0.4)),
    AxisKey.FORMAL: ((0.0, 0.8), (3.0, 5.0)),
    AxisKey.INFORMAL: ((2.0, 3.6), (0.0, 0.2)),
}

# The falsification battery. Axis 8 (exclamations_per_100s) is DELIBERATELY
# ABSENT: PERSONAS.md records its bands as overlapping on [0.0, 0.4] and
# states it is "measured and REPORTED but is NOT part of the falsification
# battery." Axes 1a/1b (p25/p75) are reported alongside axis 1 and are "not
# counted as separate axes", so they are absent here too.
BATTERY: Final[tuple[tuple[str, AxisKey], ...]] = (
    ("1", AxisKey.MEDIAN_SENTENCE_LEN),
    ("2", AxisKey.MEAN_SENTENCES),
    ("3", AxisKey.MEAN_PARAGRAPHS),
    ("4", AxisKey.GREETING_PCT),
    ("5", AxisKey.SIGNOFF_PCT),
    ("6", AxisKey.HEDGING),
    ("7", AxisKey.CONTRACTIONS),
    ("9a", AxisKey.FORMAL),
    ("9b", AxisKey.INFORMAL),
)

BATTERY_SIZE: Final = len(BATTERY)  # 9

# Reported but excluded from the battery, with the reason.
NON_BATTERY: Final[tuple[tuple[str, AxisKey, str], ...]] = (
    ("1a", AxisKey.SENTENCE_LEN_P25, "reported alongside axis 1, not a separate axis"),
    ("1b", AxisKey.SENTENCE_LEN_P75, "reported alongside axis 1, not a separate axis"),
    ("8", AxisKey.EXCLAMATIONS, "BANDS OVERLAP on [0.0, 0.4] -- excluded by PERSONAS.md"),
)

# --------------------------------------------------------------------------
# Correspondents and rendering tables (fictional; example.com/.org only).
# Weekday/month names are fixed English tables rather than strftime, because
# %a/%b are locale-dependent and would break byte-determinism across hosts.
# --------------------------------------------------------------------------

CORRESPONDENTS: Final[tuple[tuple[str, str], ...]] = (
    ("Alex Turner", "alex.turner@example.com"),
    ("Priya Nandel", "priya.nandel@example.com"),
    ("Owen Brackett", "owen.brackett@example.org"),
    ("Lena Vosk", "lena.vosk@example.org"),
    ("Marcus Hale", "marcus.hale@example.com"),
    ("Tessa Groh", "tessa.groh@example.org"),
    ("Devon Aumary", "devon.aumary@example.com"),
    ("Ruth Callender", "ruth.callender@example.org"),
)

WEEKDAYS: Final[tuple[str, ...]] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS: Final[tuple[str, ...]] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# --------------------------------------------------------------------------
# Inbound greeting distribution (PERSONAS.md: 20/20/25/15/20) and Margaret's
# deterministic mirroring map (two outputs only) - the issue-#83 ground truth.
# --------------------------------------------------------------------------

INBOUND_GREETING_FORMS: Final[tuple[tuple[GreetingForm, int], ...]] = (
    (GreetingForm.BARE_NAME, 20),
    (GreetingForm.HEY, 20),
    (GreetingForm.HI, 25),
    (GreetingForm.DEAR, 15),
    (GreetingForm.NONE, 20),
)

MARGARET_MIRROR: Final[dict[GreetingForm, GreetingForm]] = {
    GreetingForm.BARE_NAME: GreetingForm.DEAR,
    GreetingForm.HEY: GreetingForm.HI,
    GreetingForm.HI: GreetingForm.HI,
    GreetingForm.DEAR: GreetingForm.DEAR,
    GreetingForm.NONE: GreetingForm.DEAR,
}

SUBJECTS: Final[tuple[str, ...]] = (
    "Dock schedule for next week",
    "Q3 carrier rates",
    "Warehouse intake window",
    "Invoice 4471 discrepancy",
    "Route change on the northern lane",
    "Trailer availability Thursday",
    "Updated service agreement",
    "Onboarding the new account",
    "Quarterly review scheduling",
    "Freight claim follow-up",
    "Pallet count reconciliation",
    "Contract renewal timing",
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

SIG_DELIM: Final = "-- "

A_SIGNATURE: Final = "Dana Reyes\nNorthline Freight"
B_SIGNATURE: Final = (
    "Margaret Ellsworth\n"
    "Director of Client Relations\n"
    "Ellsworth & Partners\n"
    "m.ellsworth@example.org | (555) 0100"
)


class PersonaSpec(BaseModel):
    """One persona's fixed identity and mechanical rates (PERSONAS.md)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    first: str
    addr: str
    signature: str
    sig_rate: float
    """Probability a sent message carries the ``-- `` signature block."""


PERSONAS: Final[dict[Persona, PersonaSpec]] = {
    Persona.A: PersonaSpec(
        name="Dana Reyes",
        first="Dana",
        addr="dana.reyes@example.com",
        signature=A_SIGNATURE,
        sig_rate=0.20,
    ),
    Persona.B: PersonaSpec(
        name="Margaret Ellsworth",
        first="Margaret",
        addr="m.ellsworth@example.org",
        signature=B_SIGNATURE,
        sig_rate=1.00,
    ),
}
