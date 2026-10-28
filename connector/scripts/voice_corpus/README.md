# scripts/voice_corpus

Generates the synthetic sent-mail corpus used to exercise voice profiling
without touching a real mailbox. Never shipped (`scripts/voice_corpus/**` is on
the `never_ship` list).

- `generate.py` -- writes a corpus; `measure.py` -- scores one.
- `axes.py`, `banks.py`, `models.py`, `config.py` -- the generation dimensions,
  phrase banks, data shapes, and settings behind it.
- `corpus/` -- generated output.
- `PERSONAS.md`, `volume-curve.md` -- the personas modeled,
  how much mail is enough, and the known gaps.
