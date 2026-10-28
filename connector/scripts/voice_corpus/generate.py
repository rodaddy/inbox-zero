"""Deterministic synthetic email corpus generator - thread assembly and CLI.

Implements PERSONAS.md mechanically. No model is called and no message is
hand-written: every body is assembled from the closed banks in
``voice_corpus.banks``, driven by a single seeded ``random.Random``, so the
same seed yields a byte-identical corpus. That property is the whole point -
it is what lets a change to a profiler be attributed to the profiler rather
than to the data underneath it.

UNSOUND USE WARNING: synthetic prose carries the generating model's
fingerprints. This corpus must NEVER be cited as evidence of human voice
capture. It is sound for exactly four things: falsification, mechanics,
greeting mirroring, and the volume curve.

DETERMINISM IS THE INVARIANT ANY EDIT MUST PRESERVE
    The rng is drawn from in a fixed order. An extra draw, a skipped draw, or
    a reordering shifts the entire downstream stream, so a change that looks
    locally harmless silently rewrites every later thread. The two-run
    ``diff -r`` in the generated ``README.md`` is how the invariant is
    re-proven after any edit.

LOGGING SCOPE - a stated judgment call, not an omission
    This is a CLI instrument that writes files and exits, not a service. The
    ``logged`` decorator covers entry/exit/failure on the work and I/O
    boundaries; there is no second process to correlate with and no daemon
    lifetime to rotate over, so shipping/rotation would be structure carrying
    no signal. The rule that IS load-bearing is honoured: a run that produces
    nothing SAYS SO and exits 2, never a clean zero over an empty result.

Key Components:
    - weighted_choice: one-draw weighted pick; the draw count is contract.
    - build_inbound_body / build_a_body / build_b_body: message assembly per
      register, each returning the pre-signature core plus its ground truth.
    - generate_thread: one conversation, alternating inbound/outbound by slot
      parity, every reply quoting its immediate predecessor (depth exactly 1).
    - main: the CLI. Exit 0 wrote a corpus; exit 2 refused or failed I/O.

Pattern/Convention:
    Run from ``connector/scripts`` so the package and the uv project resolve
    together::

        uv run python -m voice_corpus.generate --seed 20260819

    Persona dispatch is ``match`` on the ``Persona`` enum; greeting rendering
    is a table keyed by ``GreetingForm``. Add behaviour as rows and cases,
    never as nested conditionals - and re-prove byte-identity after ANY edit
    that touches a draw.

Example:
    >>> import random
    >>> from voice_corpus.generate import weighted_choice
    >>> weighted_choice(random.Random(7), [("x", 1), ("y", 0)])
    'x'

See Also:
    - voice_corpus.banks: the fragment banks and sentence mixers.
    - voice_corpus.measure: the harness that scores what this writes.
    - PERSONAS.md: the spec every number here implements.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import BaseModel, ConfigDict

from inbox_zero_connector.utils import logged
from voice_corpus.banks import (
    B_SIGNOFFS,
    INBOUND_FOLLOWUPS,
    INBOUND_OPENERS,
    PERSONA_A_MIXER,
    PERSONA_B_MIXER,
    compose_sentence,
)
from voice_corpus.config import (
    CORRESPONDENTS,
    DEFAULT_SEED,
    INBOUND_GREETING_FORMS,
    MARGARET_MIRROR,
    MONTHS,
    PERSONAS,
    SIG_DELIM,
    SUBJECTS,
    THREADS_PER_PERSONA,
    WEEKDAYS,
    CorpusPaths,
    GreetingForm,
    Persona,
    PersonaSpec,
    configure_logging,
)
from voice_corpus.models import Thread

FOLLOWUP_RATE: Final = 0.6
"""Chance an inbound message carries a second, follow-up sentence."""

A_GREETING_RATE: Final = 0.15
"""Axis 4: Dana greets on 15%, drawn independently of the inbound form."""

A_TWO_PARAGRAPH_RATE: Final = 0.10
"""Chance a multi-sentence Dana message splits into two paragraphs."""

REPLY_GAP_MINUTES: Final = (17, 380)
"""Inclusive randint bounds for the gap between consecutive messages."""

EPOCH: Final = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
"""Thread-start anchor. Fixed, never wall-clock: dates are corpus bytes."""

THREAD_STAGGER_MINUTES: Final = 613
PERSONA_B_OFFSET_HOURS: Final = 3

DRAW_UNIQUE_TRIES: Final = 40

A_SENTENCE_WEIGHTS: Final = ((1, 20), (2, 32), (3, 30), (4, 18))
B_SENTENCE_WEIGHTS: Final = ((7, 18), (8, 22), (9, 24), (10, 22), (11, 14))
B_PARAGRAPH_WEIGHTS: Final = ((3, 40), (4, 40), (5, 20))
THREAD_LENGTH_WEIGHTS: Final = ((2, 60), (4, 30), (6, 10))

GREETING_TEMPLATES: Final[dict[GreetingForm, str]] = {
    GreetingForm.BARE_NAME: "{first},",
    GreetingForm.HEY: "Hey {first},",
    GreetingForm.HI: "Hi {first},",
    GreetingForm.DEAR: "Dear {first},",
    GreetingForm.NONE: "",
}
"""The closed inbound greeting renderings. Indexing raises on an unknown
form, which is correct: an unlisted form is a generator bug, not a case to
absorb into an empty greeting that skews the mirroring ground truth."""


def weighted_choice[T](rng: random.Random, pairs: tuple[tuple[T, int], ...]) -> T:
    """Pick from (value, weight) pairs using exactly ONE rng draw.

    One draw, not a per-item loop of draws: the draw count is part of the
    determinism contract, so a "clearer" rewrite that consumes a different
    number of values from the stream would rewrite the corpus.
    """
    total = sum(w for _, w in pairs)
    roll = rng.uniform(0.0, float(total))
    upto = 0.0
    for value, weight in pairs:
        upto += weight
        if roll <= upto:
            return value
    return pairs[-1][0]


def format_attribution(when: datetime, display: str, addr: str) -> str:
    """Render ``On Tue, 12 Aug 2026 at 09:14, Alex Turner <a@example.com> wrote:``.

    Built from fixed English abbreviation tables rather than ``strftime``,
    because ``%a``/``%b`` are LOCALE-DEPENDENT: the same seed on a machine
    with a different locale would emit different bytes, which is exactly the
    determinism failure this corpus exists to rule out.
    """
    day = WEEKDAYS[when.weekday()]
    mon = MONTHS[when.month - 1]
    return (
        f"On {day}, {when.day} {mon} {when.year} at "
        f"{when.hour:02d}:{when.minute:02d}, {display} <{addr}> wrote:"
    )


def quote_block(body: str) -> str:
    """Prefix each line with '> '; empty lines get a bare '>'.

    Depth is always exactly one (a documented bench-fixture simplification),
    so a stripper that passes here may still be wrong on real nested mail.
    """
    return "\n".join("> " + line if line else ">" for line in body.split("\n"))


def build_inbound_body(
    rng: random.Random, form: GreetingForm, first: str, sender_first: str
) -> str:
    """Assemble one inbound message.

    Inbound prose is deliberately neutral and is NOT scored by the axis
    table. It exists so the corpus carries the inbound half of each thread -
    the half neither real corpus has, without which the greeting mirroring
    rule (issue #83) is underivable.
    """
    greeting = GREETING_TEMPLATES[form].format(first=first)
    body = [rng.choice(INBOUND_OPENERS)]
    # The follow-up draw happens ONLY on the true arm - conditional-expression
    # short-circuit is the exact draw pattern the reference corpus was built
    # with, and both draws are stream bytes.
    extra = [rng.choice(INBOUND_FOLLOWUPS)] if rng.random() < FOLLOWUP_RATE else []
    paras = [p for p in (greeting, " ".join(body + extra), sender_first) if p]
    return "\n\n".join(paras)


def _draw_unique(
    rng: random.Random,
    mixer: tuple[tuple[float, object], ...],
    n: int,
) -> list[str]:
    """Draw n sentences from a mixer, never repeating one within one message.

    A hard constraint, not a probability: a real writer does not emit the
    same sentence twice in one message, and internal repeats are the visible
    symptom of a bank too thin to support the volume curve. On exhausting the
    retry budget the slot is DROPPED rather than filled with a duplicate - a
    slightly shorter message is a smaller lie than a repeated one.
    """
    out: list[str] = []
    seen: set[str] = set()
    for _ in range(n):
        for _attempt in range(DRAW_UNIQUE_TRIES):
            cand = compose_sentence(rng, mixer)  # type: ignore[arg-type]  # mixer rows narrow at the call sites
            if cand not in seen:
                break
        else:
            continue
        seen.add(cand)
        out.append(cand)
    return out


def build_a_body(rng: random.Random, correspondent_first: str) -> tuple[str, bool]:
    """Dana. Returns (body_without_signature, has_greeting).

    Greeting is drawn INDEPENDENTLY of the inbound form at 15% (axis 4) -
    the ground truth a mirroring detector must fail to find a dependence in.
    Never a sign-off (axis 5 = 0%).
    """
    lines: list[str] = []
    has_greeting = rng.random() < A_GREETING_RATE
    if has_greeting:
        lines.append(f"{correspondent_first},")

    n_sent = weighted_choice(rng, A_SENTENCE_WEIGHTS)
    sentences = _draw_unique(rng, PERSONA_A_MIXER, n_sent)
    sentences = [s if s.endswith("!") else s + "." for s in sentences]

    paras = [" ".join(sentences)]
    # Draw order is contract: the paragraph roll happens ONLY when the
    # short-circuit reaches it, and the cut draw only inside the true arm.
    if len(sentences) >= 2 and rng.random() < A_TWO_PARAGRAPH_RATE:
        cut = rng.randint(1, len(sentences) - 1)
        paras = [" ".join(sentences[:cut]), " ".join(sentences[cut:])]

    lines.extend(paras)
    return "\n\n".join(lines), has_greeting


def build_b_body(
    rng: random.Random, inbound_form: GreetingForm, correspondent_first: str
) -> tuple[str, GreetingForm]:
    """Margaret. Returns (body_without_signature, outbound_greeting_form).

    Greeting is a DETERMINISTIC function of the inbound form (mirroring with
    an escalation floor) - the positive half of the issue-#83 ground truth.
    Sign-off on 100% of messages (axis 5).
    """
    out_form = MARGARET_MIRROR[inbound_form]
    greeting_word = "Dear" if out_form is GreetingForm.DEAR else "Hi"
    greeting = f"{greeting_word} {correspondent_first},"

    n_sent = weighted_choice(rng, B_SENTENCE_WEIGHTS)
    sentences = [s + "." for s in _draw_unique(rng, PERSONA_B_MIXER, n_sent)]

    n_para = min(weighted_choice(rng, B_PARAGRAPH_WEIGHTS), len(sentences))
    per = len(sentences) // n_para
    paras = []
    idx = 0
    for p in range(n_para):
        take = per if p < n_para - 1 else len(sentences) - idx
        paras.append(" ".join(sentences[idx : idx + take]))
        idx += take

    signoff = weighted_choice(rng, B_SIGNOFFS)
    blocks = [greeting, *paras, f"{signoff}\nMargaret Ellsworth"]
    return "\n\n".join(blocks), out_form


def assemble_wire(core: str, signature: str | None, quoted: tuple[str, str] | None) -> str:
    """Join body + optional signature block + optional quoted reply.

    This is the exact inverse of what a stripper must undo, which is why
    ``expected_stripped_body`` records ``core`` on the way through: the
    answer key is written by the same code that builds the puzzle.
    """
    sig_part = None if signature is None else f"{SIG_DELIM}\n{signature}"
    quote_part = None if quoted is None else f"{quoted[0]}\n{quote_block(quoted[1])}"
    return "\n\n".join(part for part in (core, sig_part, quote_part) if part is not None)


class ReplyContext(BaseModel):
    """What an outbound reply needs to know about the message it answers.

    Bundling these keeps the reply path a single typed handoff: the thread
    loop, which SAW the inbound message being built, passes the facts forward
    instead of the reply digging them back out of a loose dict.
    """

    model_config = ConfigDict(frozen=True)

    inbound_form: GreetingForm
    """The greeting form being replied to - the mirroring input (axis 4)."""

    body: str
    """The full wire body being quoted below the reply."""

    display: str
    """Display name of the quoted message's author, for the attribution line."""

    addr: str
    """Address of the quoted message's author, for the attribution line."""

    when: datetime
    """When the quoted message was sent, for the attribution line."""


def _inbound_message(
    rng: random.Random,
    persona: PersonaSpec,
    corr: tuple[str, str],
    when: datetime,
    prev: tuple[str, datetime] | None,
) -> tuple[dict[str, object], GreetingForm]:
    """Build one inbound message dict plus the greeting form it drew."""
    display, addr = corr
    form = weighted_choice(rng, INBOUND_GREETING_FORMS)
    core = build_inbound_body(rng, form, persona.first, display.split()[0])
    quoted = (
        None if prev is None else (format_attribution(prev[1], persona.name, persona.addr), prev[0])
    )
    message: dict[str, object] = {
        "direction": "inbound",
        "from": addr,
        "to": persona.addr,
        "date": when.isoformat(),
        "body": assemble_wire(core, None, quoted),
        "ground_truth": {"greeting_form": form.value},
    }
    return message, form


def _outbound_message(
    rng: random.Random,
    persona_key: Persona,
    corr: tuple[str, str],
    when: datetime,
    reply: ReplyContext,
) -> dict[str, object]:
    """Build one outbound message dict, with its full ground-truth block."""
    persona = PERSONAS[persona_key]
    display, addr = corr
    corr_first = display.split()[0]
    inbound_form = reply.inbound_form

    match persona_key:
        case Persona.A:
            core, has_greeting = build_a_body(rng, corr_first)
            out_form = GreetingForm.BARE_NAME if has_greeting else GreetingForm.NONE
            has_signoff = False
        case Persona.B:
            core, out_form = build_b_body(rng, inbound_form, corr_first)
            has_signoff = True

    has_sig = rng.random() < persona.sig_rate
    signature = persona.signature if has_sig else None
    quoted = (format_attribution(reply.when, reply.display, reply.addr), reply.body)
    return {
        "direction": "outbound",
        "from": persona.addr,
        "to": addr,
        "date": when.isoformat(),
        "body": assemble_wire(core, signature, quoted),
        "ground_truth": {
            "greeting_form": out_form.value,
            "has_signoff": has_signoff,
            "has_signature": has_sig,
            "inbound_greeting_form": inbound_form.value,
            "expected_stripped_body": core + "\n",
        },
    }


def generate_thread(
    rng: random.Random, persona_key: Persona, index: int, start: datetime
) -> dict[str, object]:
    """Build one whole thread: alternating inbound/outbound, oldest first.

    Slot parity IS the direction (even = inbound), so a thread always opens
    with the correspondent and the persona is always replying to something -
    which is what makes the mirroring question well-posed for every outbound
    message in the corpus.
    """
    persona = PERSONAS[persona_key]
    corr = rng.choice(CORRESPONDENTS)
    subject = rng.choice(SUBJECTS)

    n_messages = weighted_choice(rng, THREAD_LENGTH_WEIGHTS)

    messages: list[dict[str, object]] = []
    when = start
    prev_wire: str | None = None
    prev_when = when
    # Slot 0 is always inbound, so this is always overwritten before a reply
    # reads it; the initial value only satisfies definite assignment.
    inbound_form = GreetingForm.NONE

    for slot in range(n_messages):
        if slot > 0:
            when = when + timedelta(minutes=rng.randint(*REPLY_GAP_MINUTES))

        match slot % 2:
            case 0:
                prev = None if prev_wire is None else (prev_wire, prev_when)
                message, inbound_form = _inbound_message(rng, persona, corr, when, prev)
                messages.append(message)
            case _:
                display, addr = corr
                reply = ReplyContext(
                    inbound_form=inbound_form,
                    body=str(messages[-1]["body"]),
                    display=display,
                    addr=addr,
                    when=prev_when,
                )
                messages.append(_outbound_message(rng, persona_key, corr, when, reply))

        prev_wire = str(messages[-1]["body"])
        prev_when = when

    return {
        "thread_id": f"{persona_key.value.lower()}-{index:04d}",
        "persona": persona_key.value,
        "subject": subject,
        "messages": messages,
    }


README: Final = """# Synthetic email corpus — BENCH FIXTURE

Generated by `../generate.py` from the ground-truth spec in `../PERSONAS.md`.

## Regenerate

    python3 generate.py --seed {seed} --out corpus

Same seed in, byte-identical corpus out. The seed is recorded in
`MANIFEST.json`.

## THE UNSOUND USE — read before citing this corpus for anything

Synthetic prose carries the fingerprints of the model that generated it (and
here, of the fragment banks a script assembles). **This corpus must NEVER be
cited as evidence of human voice capture.** A profiler that "captures the
voice" of Dana or Margaret has recognised a template, not a person. The
circularity is sharpest when the same model family both generates and
profiles.

## The four sound uses — and nothing else

1. **Falsification.** Two deliberately opposed personas must yield two
   distinguishable profiles. Clean synthetic is the easiest possible input, so
   a blend here proves failure everywhere. A pass proves only that the floor
   was cleared.
2. **Mechanics.** Thread parsing, signature stripping, quoted-reply removal,
   greeting extraction. Ground truth is an advantage: we wrote the answers.
   Every outbound message carries `expected_stripped_body`, which is the exact
   correct output after BOTH signature stripping and quoted-reply removal.
3. **Greeting mirroring (issue #83).** This is the only corpus carrying the
   INBOUND half of each thread. Persona A does NOT mirror (her greeting is
   statistically independent of the inbound form); Persona B mirrors
   deterministically with an escalation floor. A detector reporting A as a
   mirrorer has produced a false positive.
4. **Volume curve (issue #81).** Profiles at 10/25/50/75/100/200 messages, to
   find where they stabilise. The widely-repeated "75 emails per author"
   figure is UNVERIFIED — no paper reports it. Measure the curve.

## Layout

- `persona-a/thread-NNNN.json`, `persona-b/thread-NNNN.json`
- `MANIFEST.json` — seed, counts.

Every name, address, employer, and fact is FICTIONAL. Addresses are
`example.com` / `example.org` only. No content from any real corpus appears.

## Known simplifications (deliberate — this is a bench fixture)

- Quoting nesting depth is exactly ONE. A stripper that only handles depth 1
  passes here and may still be wrong on real mail.
- Subjects and topics are repetitive and obviously templated. Realism was
  spent only where the axis table measures it.
- Axis 8 (exclamation marks) is a KNOWN overlap between the two personas and
  is deliberately NOT part of the falsification battery.
"""


@logged
def _write_persona(rng: random.Random, out: Path, key: Persona, threads: int) -> dict[str, int]:
    """Write one persona's threads and return its message tallies."""
    pdir = out / f"persona-{key.value.lower()}"
    pdir.mkdir(parents=True, exist_ok=True)
    inbound = outbound = 0
    for i in range(1, threads + 1):
        offset_hours = 0 if key is Persona.A else PERSONA_B_OFFSET_HOURS
        start = EPOCH + timedelta(minutes=(i - 1) * THREAD_STAGGER_MINUTES, hours=offset_hours)
        thread = generate_thread(rng, key, i, start)
        # Validate on the way OUT, not just on the way in. The generator is
        # the only writer of this schema, so a shape defect introduced here
        # would otherwise surface 200 files later inside the harness.
        validated = Thread.model_validate(thread)
        path = pdir / f"thread-{i:04d}.json"
        path.write_text(
            json.dumps(thread, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        inbound += sum(1 for m in validated.messages if m.direction == "inbound")
        outbound += sum(1 for m in validated.messages if m.direction == "outbound")
    return {
        "threads": threads,
        "messages": inbound + outbound,
        "inbound": inbound,
        "outbound": outbound,
    }


@logged
def _write_corpus(out: Path, seed: int, threads: int) -> dict[str, dict[str, int]]:
    """Write both personas, the manifest, and the corpus README."""
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    manifest_personas = {
        key.value: _write_persona(rng, out, key, threads) for key in (Persona.A, Persona.B)
    }
    manifest = {
        "seed": seed,
        "generator": "generate.py",
        "spec": "PERSONAS.md",
        "generated_at": "deterministic-build-no-wallclock",
        "personas": manifest_personas,
        "warning": (
            "SYNTHETIC. Never cite as evidence of human voice capture. "
            "Sound uses: falsification, mechanics, greeting mirroring, "
            "volume curve."
        ),
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "README.md").write_text(README.format(seed=seed), encoding="utf-8")
    return manifest_personas


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the CLI. Split out so ``main`` stays inside the size ceiling."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", type=Path, default=CorpusPaths().corpus)
    ap.add_argument("--threads", type=int, default=THREADS_PER_PERSONA)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Generate the corpus. Exit 0 on success, 2 on a refusal or an IO failure.

    GUARD: a non-positive ``--threads`` would produce an empty corpus while
    every count printed zero and the exit code said success. Absence of
    output is never allowed to read as success here, so that case is refused
    loudly.
    """
    args = _parse_args(argv)
    configure_logging()
    logger.debug("generate entry seed={} threads={} out={}", args.seed, args.threads, args.out)

    if args.threads < 1:
        logger.warning(
            "REFUSED: --threads={} would write an empty corpus; a run that "
            "produces nothing must not report success",
            args.threads,
        )
        return 2

    try:
        manifest_personas = _write_corpus(args.out, args.seed, args.threads)
    except OSError as exc:
        # The @logged decorator already recorded the traceback at ERROR; this
        # boundary turns it into the operator-facing refusal and exit code.
        print(f"generate.py: {exc}", file=sys.stderr)
        return 2

    for key, c in manifest_personas.items():
        print(
            f"persona-{key.lower()}: threads={c['threads']} "
            f"messages={c['messages']} inbound={c['inbound']} "
            f"outbound={c['outbound']}"
        )
    logger.info(
        "generate exit seed={} personas={} messages={}",
        args.seed,
        len(manifest_personas),
        sum(c["messages"] for c in manifest_personas.values()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
