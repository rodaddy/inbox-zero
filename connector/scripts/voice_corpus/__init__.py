"""Voice Corpus - deterministic synthetic email corpus generator and battery.

Two instruments over one fixture. ``generate`` assembles a synthetic
two-persona email corpus from closed fragment banks driven by a single seeded
``random.Random``, so the same seed yields a byte-identical corpus.
``measure`` scores that corpus against the nine-axis table in ``PERSONAS.md``
and answers two independent questions that are never collapsed into one
number: IN-BAND (did the generator hit its own spec?) and SEPARATED (are the
two personas distinguishable?). Determinism is the architecture: no model is
called, no wall clock is read into the data, and every byte of the corpus is
a function of the seed - which is what lets a change to a profiler be
attributed to the profiler rather than to the data underneath it.

The package follows the connector's own standards mechanically. All
configuration - paths, the seed, the closed word lists, the tolerance bands,
the persona fixtures - lives in ``config``; no other module hardcodes a
value both instruments must agree on. Every wire and result shape is a
pydantic v2 model. Branching is tables and ``match`` over ``StrEnum`` members,
never nested conditionals. Every I/O boundary sits under the ``logged``
decorator (entry/exit at DEBUG with duration, failure at ERROR with the
traceback, then re-raise) with named exceptions, so a failed run is
diagnosable from stderr alone. Logging sinks come from the connector's own
``inbox_zero_connector.utils.logging_config`` engine - this package
deliberately ships no second logging implementation.

UNSOUND USE WARNING: synthetic prose carries the generating model's
fingerprints. This corpus must NEVER be cited as evidence of human voice
capture. It is sound for exactly four things: falsification, mechanics,
greeting mirroring, and the volume curve. ``PERSONAS.md`` in this directory
is the ground-truth spec; ``volume-curve.md`` is the
measured findings carried with the instrument that produced them.

Key Components:
    - config: the keystone. Seed, paths, closed word lists, tolerance bands,
      the falsification battery, persona fixtures, enums, and the one
      ``configure_logging`` call that points loguru at the connector engine.
    - models: the wire schema (Thread, Message, GroundTruth, Manifest) and
      the measurement result (AxisResult). Pydantic v2, ``extra="forbid"``.
    - (flow decorator) ``logged`` now lives in ``inbox_zero_connector.utils.observed``;
      every I/O boundary here still wears it.
    - banks: the closed fragment banks and slot-template sentence builders.
      Draw order from the shared rng is this module's public contract.
    - generate: thread assembly and the generator CLI. Exit 0 wrote a corpus,
      exit 2 refused (an empty corpus must never read as success).
    - axes: the measurement core - stripping, greeting/sign-off detection,
      sentence segmentation, the per-axis tally over outbound messages.
    - measure: discrimination, rendering, and the harness CLI. Exit 0 printed
      a measurement, exit 2 refused (missing or malformed corpus).

Pattern/Convention:
    Run both instruments through uv from this package's parent directory
    (``connector/scripts``), so the project environment and the package root
    resolve together::

        cd kits/inbox-zero/connector/scripts
        uv run python -m voice_corpus.generate
        uv run python -m voice_corpus.measure --corpus voice_corpus/corpus --discriminate

    Extend by adding rows, not branches: a new sentence kind is a row in a
    mixer table in ``banks``, a new axis is a row in ``config.BANDS`` plus a
    field on ``models.AxisResult``. The rng draw order is the determinism
    contract - an added, removed, or reordered draw rewrites every later
    thread, so any generator edit must be re-proven byte-identical or be
    declared a NEW corpus with new recorded numbers.

Example:
    >>> import random
    >>> from voice_corpus.banks import PERSONA_A_MIXER, compose_sentence
    >>> compose_sentence(random.Random(20260819), PERSONA_A_MIXER)
    'the pallet count clears the 14th.'

See Also:
    - PERSONAS.md: the ground-truth spec both instruments implement.
    - volume-curve.md: the measured stabilisation curve (issue #81).
    - src/inbox_zero_connector/utils/logging_config.py: the logging engine.
"""
