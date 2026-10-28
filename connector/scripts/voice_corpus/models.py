"""The corpus wire schema and the measurement result shapes, as pydantic v2.

WHY MODELS RATHER THAN DICTS
    ``generate`` writes this JSON and ``measure`` reads it. While both used
    loose ``dict[str, Any]`` they agreed on the shape only by convention - a
    renamed key would have surfaced as a silent ``None`` in a measurement
    rather than as an error at the boundary. Typing the schema makes the two
    agree by construction: the generator cannot emit a shape the harness
    cannot parse, because the same class defines both directions.

WHY THE SERIALISED CORPUS IS STILL HAND-BUILT IN ``generate``
    Determinism. The corpus on disk is ``json.dumps(..., sort_keys=True)`` of
    a plain dict, and it must stay byte-identical at seed 20260819. These
    models validate and describe that shape; they are deliberately NOT the
    thing that renders it, so no pydantic serialisation default can ever move
    a byte in the committed-to corpus.

FIELD ALIASES
    ``from`` and ``to`` are the wire keys and ``from`` is a Python keyword,
    so the attributes are ``sender``/``recipient`` with aliases.
    ``populate_by_name`` is on, so both spellings construct.

Key Components:
    - GroundTruth: what the generator KNOWS about a message - the answer key
      a mechanics test is scored against.
    - Message / Thread: one wire message; one conversation.
    - PersonaCounts / Manifest: the MANIFEST.json shape, warning included.
    - AxisResult: one persona's measured nine-axis table.
    - BatteryRow / Discrimination: the falsification verdict, both questions
      (IN-BAND and SEPARATED) carried side by side so a renderer cannot show
      one without the other.

Pattern/Convention:
    Validate at every boundary: ``generate`` validates each thread on the way
    OUT (a shape defect fails at the writer, not 200 files later inside the
    harness), and ``measure`` validates each file on the way IN. Extend by
    adding a field here first; both instruments then disagree loudly until
    they agree again.

Example:
    >>> from voice_corpus.models import GroundTruth
    >>> GroundTruth(greeting_form="none").greeting_form
    'none'

See Also:
    - voice_corpus.generate: the only writer of this schema.
    - voice_corpus.axes: the reader, via Thread.model_validate.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GroundTruth(BaseModel):
    """What the generator KNOWS about a message, recorded alongside it.

    Ground truth is the whole reason a synthetic corpus is worth building: we
    wrote the answers, so a mechanics test (signature stripping, quoted-reply
    removal, greeting extraction) can be scored exactly rather than eyeballed.

    Every field except ``greeting_form`` is outbound-only, hence optional -
    an inbound message carries only the greeting form it used, which is the
    input half of the mirroring question (issue #83).
    """

    model_config = ConfigDict(extra="forbid")

    greeting_form: str
    """One of the closed forms: bare_name, hey, hi, dear, none."""

    has_signoff: bool | None = None
    """Whether a closing block was emitted. Axis 5 ground truth."""

    has_signature: bool | None = None
    """Whether a ``-- `` signature block was appended. Mechanics ground truth."""

    inbound_greeting_form: str | None = None
    """The form this message was replying TO - the mirroring input."""

    expected_stripped_body: str | None = None
    """The exact correct body after BOTH signature and quoted-reply removal.

    This is the known-correct answer a stripper is scored against, and the
    value ``measure`` prefers over running its own fallback stripper.
    """


class Message(BaseModel):
    """One message on the wire, inbound or outbound."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    direction: str
    """``inbound`` or ``outbound``. Only outbound messages are measured."""

    sender: str = Field(alias="from")
    recipient: str = Field(alias="to")
    date: str
    """ISO-8601 with offset. A string, not a datetime, because the on-disk
    corpus must round-trip byte-identically and datetime parsing normalises."""

    body: str
    """The FULL wire body: prose, optional signature block, optional quote."""

    ground_truth: GroundTruth


class Thread(BaseModel):
    """One conversation: alternating inbound/outbound messages, one subject."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    persona: str
    subject: str
    messages: list[Message]


class PersonaCounts(BaseModel):
    """Per-persona message tallies, written into ``MANIFEST.json``."""

    model_config = ConfigDict(extra="forbid")

    threads: int
    messages: int
    inbound: int
    outbound: int


class Manifest(BaseModel):
    """``MANIFEST.json`` - what seed produced this corpus, and how much of it.

    The ``warning`` field is not decoration. It travels with the data so that
    a corpus copied out of this directory still carries the standing limit on
    how it may be cited.
    """

    model_config = ConfigDict(extra="forbid")

    seed: int
    generator: str
    spec: str
    generated_at: str
    personas: dict[str, PersonaCounts]
    warning: str


class AxisResult(BaseModel):
    """One persona's measured nine-axis table (plus 1a/1b and axis 8).

    Field names ARE the axis keys (``config.AxisKey`` values), so a band
    lookup and a result read use one spelling. Every rate is pre-rounded to
    four places by the tally - these are the exact numbers the receipts
    record, so the rounding is part of the contract, not presentation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages_measured: int
    total_words: int
    total_sentences: int
    median_sentence_len_words: float
    sentence_len_p25_words: float
    sentence_len_p75_words: float
    mean_sentences_per_message: float
    mean_paragraphs_per_message: float
    greeting_pct: float
    greeting_form_distribution_pct: dict[str, float]
    signoff_pct: float
    signoff_form_distribution_pct: dict[str, float]
    hedging_per_100w: float
    contractions_per_100w: float
    exclamations_per_100s: float
    formal_per_100w: float
    informal_per_100w: float

    def value(self, key: str) -> float:
        """Read one axis value by its ``AxisKey`` string."""
        out: float = getattr(self, key)
        return out


class BatteryRow(BaseModel):
    """One verdict row: both values, both bands, both questions, side by side."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    axis: str
    measure: str
    a_value: float
    a_band: list[float]
    a_conforms: bool
    b_value: float
    b_band: list[float]
    b_conforms: bool
    separated: bool
    in_battery: bool
    excluded_because: str | None = None


class Discrimination(BaseModel):
    """The full battery verdict: rows plus the two independent counts.

    ``separated_count`` counts battery axes only; ``in_band_count`` counts
    every band check in the battery (both personas x nine measures = 18).
    The separation RULE travels with the numbers - a bare "9/9" invites
    someone to tune the rule until it reads nine.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: list[BatteryRow]
    separated_count: int
    battery_size: int
    in_band_count: int
    in_band_total: int
    nonconforming: list[str]
    separation_rule: str
