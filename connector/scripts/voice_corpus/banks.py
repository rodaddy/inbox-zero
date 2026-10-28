"""Closed fragment banks, slot-template sentence builders, and the mixers.

SENTENCES ARE COMPOSED, NOT DRAWN WHOLE
    Each builder fills a template's ``{t}``/``{t2}``/``{a}`` slots from the
    shared noun banks, so the combinatorial space per template is the product
    of its slot cardinalities. That is what keeps any one sentence rare and
    stops the volume curve (issue #81) from measuring bank exhaustion instead
    of voice convergence. An earlier fixed-pool version repeated its top
    sentence 241 times; the slot form collapsed that to 59 (volume-curve.md).

DETERMINISM CONTRACT
    Every function here takes the caller's seeded ``random.Random`` and draws
    from it in a fixed order. Adding, removing, or reordering a draw - even
    one that cannot change a single sentence in isolation - shifts the whole
    downstream stream and rewrites the corpus. Treat the draw order as this
    module's public contract, not an implementation detail. ``_fill`` always
    draws all three slots for exactly this reason.

THE MIXERS ARE TABLES, NOT BRANCHES
    Each persona's sentence-kind distribution is a cumulative-threshold table
    consumed by ``compose_sentence``: one rng draw, walked against ascending
    thresholds. Retuning a persona is editing rows; the walking code never
    changes, and the one-draw contract is enforced by the single consumer.

UNSOUND USE WARNING
    Synthetic prose carries the fingerprints of whatever produced it - here,
    of these banks. Output assembled from this module must NEVER be cited as
    evidence of human voice capture.

Key Components:
    - THINGS / ACTORS / DAYS / TIMES: the shared register-neutral noun banks.
    - a_sentence_*: Persona A builders, one closed-list register each, so
      axis 7 (contractions) tunes independently of axis 9b (informal).
    - b_sentence_*: Persona B builders - short/mid/long formal composition
      plus the drawn-whole closers.
    - PERSONA_A_MIXER / PERSONA_B_MIXER: the cumulative-threshold tables.
    - compose_sentence: the single table-walker both personas share.
    - INBOUND_OPENERS / INBOUND_FOLLOWUPS: neutral inbound prose (unmeasured).
    - B_SIGNOFFS: Margaret's weighted closing lines (60/25/15).

Pattern/Convention:
    Add a sentence kind by adding a builder and a mixer row; retune rates by
    editing thresholds. Never make ``_fill``'s three draws conditional, and
    never reorder a builder's draws - byte-identity at seed 20260819 is the
    property every edit here must re-prove.

Example:
    >>> import random
    >>> from voice_corpus.banks import PERSONA_B_MIXER, compose_sentence
    >>> sentence = compose_sentence(random.Random(1), PERSONA_B_MIXER)
    >>> sentence.split()[0]
    'It'

See Also:
    - voice_corpus.generate: the only caller; owns message/thread assembly.
    - PERSONAS.md: the axis targets these banks are calibrated against.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from voice_corpus.config import SignoffForm

if TYPE_CHECKING:
    import random
    from collections.abc import Callable

# --------------------------------------------------------------------------
# Inbound body fragments (neutral register; not measured by the axis table)
# --------------------------------------------------------------------------

INBOUND_OPENERS: Final[tuple[str, ...]] = (
    "Are you free Tuesday to walk through the dock schedule?",
    "Quick question about the carrier rates we discussed.",
    "Wanted to check in on the intake window for next week.",
    "Do you have a moment to look at invoice 4471?",
    "Following up on the route change we talked about.",
    "Checking whether trailers are available Thursday.",
    "Can you confirm the numbers on the pallet count?",
    "Is the renewal date still where we left it?",
)

INBOUND_FOLLOWUPS: Final[tuple[str, ...]] = (
    "Let me know what works on your end.",
    "No rush, but I would like to close this out this week.",
    "Happy to jump on a call if that is easier.",
    "I can send over the paperwork once you confirm.",
    "The team is waiting on this before they schedule.",
    "Anything you need from me to move it along?",
)

B_SIGNOFFS: Final[tuple[tuple[SignoffForm, int], ...]] = (
    (SignoffForm.BEST_REGARDS, 60),
    (SignoffForm.KIND_REGARDS, 25),
    (SignoffForm.WITH_APPRECIATION, 15),
)

# --------------------------------------------------------------------------
# Shared slot banks, register-neutral.
# --------------------------------------------------------------------------

THINGS: Final[tuple[str, ...]] = (
    "dock schedule",
    "intake window",
    "pallet count",
    "carrier rate",
    "trailer booking",
    "freight claim",
    "manifest",
    "pickup time",
    "renewal date",
    "service agreement",
    "route change",
    "invoice",
    "delivery window",
    "unloading slot",
    "rate schedule",
    "shipment record",
    "billing cycle",
    "inspection report",
    "yard assignment",
    "load plan",
)

ACTORS: Final[tuple[str, ...]] = (
    "dispatch",
    "the yard",
    "accounting",
    "the carrier",
    "the warehouse",
    "our operations team",
    "the adjuster",
    "the night crew",
    "scheduling",
    "the broker",
    "the terminal",
    "the compliance desk",
)

DAYS: Final[tuple[str, ...]] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "the start of next week",
    "the end of the week",
    "the 14th",
    "early next month",
    "the following monday",
)

TIMES: Final[tuple[str, ...]] = (
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "noon",
    "two",
    "three",
    "four",
    "five",
    "six",
)

# --------------------------------------------------------------------------
# Persona A builders. Each carries exactly one closed-list register (or
# none), so each axis rate tunes independently of the others.
# --------------------------------------------------------------------------


def a_sentence_plain(rng: random.Random) -> str:
    """Persona A, marker-free. Terse: 3-9 words."""
    t = rng.choice(THINGS)
    a = rng.choice(ACTORS)
    d = rng.choice(DAYS)
    tm = rng.choice(TIMES)
    n = rng.randint(2, 9) * 20
    return rng.choice([
        f"{a} moved the {t} to {d}",
        f"the {t} clears {d}",
        f"{a} has the {t}",
        f"{t} is set for {tm}",
        f"still waiting on {a}",
        f"the {t} closed out {d}",
        f"{a} wants the {t} {d}",
        f"{n} units on the {t}",
        f"{t} goes out {d}",
        f"sent the {t} to {a}",
        f"the {t} slipped to {d}",
        f"{a} confirmed {tm}",
        f"no change on the {t}",
        f"{t} is with {a}",
        f"pushed the {t} to {d}",
    ])


def a_sentence_contraction(rng: random.Random) -> str:
    """Persona A, carries exactly one contraction and NO informal marker."""
    t = rng.choice(THINGS)
    a = rng.choice(ACTORS)
    d = rng.choice(DAYS)
    return rng.choice([
        f"I'll pull the {t}",
        f"I've moved the {t} to {d}",
        f"we're covered on the {t}",
        f"it's booked for {d}",
        f"that's the {t} handled",
        f"{a} hasn't confirmed the {t}",
        f"the {t} doesn't match our records",
        f"I don't have the {t} yet",
        f"{a} didn't log the {t}",
        f"we've pushed the {t} to {d}",
        f"that won't clear before {d}",
        f"there's a gap in the {t}",
        f"the {t} isn't ready",
        f"they haven't released the {t}",
        f"we'll close the {t} {d}",
        f"{a} wasn't told about the {t}",
        f"I'm holding the {t}",
        f"the {t} can't move before {d}",
        f"it isn't on the {t}",
        f"{a} couldn't reach the {t}",
    ])


def a_sentence_informal(rng: random.Random) -> str:
    """Persona A, carries exactly one informal marker and NO contraction."""
    t = rng.choice(THINGS)
    a = rng.choice(ACTORS)
    d = rng.choice(DAYS)
    return rng.choice([
        f"yeah the {t} works",
        f"ok sending the {t} now",
        f"got it on the {t}",
        f"sure go ahead with the {t}",
        f"fine to move the {t} to {d}",
        f"nope not before {d}",
        f"yep the {t} is confirmed",
        f"thanks for the {t}",
        f"just ping {a} about the {t}",
        f"heads up the {t} shifted to {d}",
        f"quick note on the {t}",
        f"FYI {a} changed the {t}",
        f"grab the {t} before {d}",
        f"no worries about the {t}",
        f"the guys already handled the {t}",
        f"sort out the {t} with {a}",
        f"figure out the {t} by {d}",
        f"okay on the {t}",
    ])


def a_sentence_hedge(rng: random.Random) -> str:
    """Persona A, one hedging marker (axis 6 stays near 0.3/100w)."""
    t = rng.choice(THINGS)
    d = rng.choice(DAYS)
    return rng.choice([
        f"possibly {d}",
        f"perhaps the {t} instead",
        f"somewhat tight on the {t}",
        f"rather late for the {t}",
        f"I suspect the {t} slips",
    ])


def a_sentence_formal(rng: random.Random) -> str:
    """Persona A, one formal marker (axis 9a stays near 0.4/100w)."""
    t = rng.choice(THINGS)
    a = rng.choice(ACTORS)
    return rng.choice([
        f"regarding the {t} I will follow up",
        f"please be advised the {t} is closed",
        f"therefore the {t} stands",
        f"kindly forward the {t} to {a}",
        f"concerning the {t} nothing changed",
    ])


def a_sentence_exclaim(rng: random.Random) -> str:
    """Persona A, exclamation-worthy content; the mixer appends the mark."""
    t = rng.choice(THINGS)
    return rng.choice([
        f"nice the {t} finally landed",
        f"good catch on the {t}",
        f"great news on the {t}",
    ])


def a_sentence_long(rng: random.Random) -> str:
    """Persona A, 10-13 words. Marker-free so it does not skew /100w rates."""
    t = rng.choice(THINGS)
    t2 = rng.choice(THINGS)
    a = rng.choice(ACTORS)
    d = rng.choice(DAYS)
    return rng.choice([
        f"I can move the {t} to {d} if {a} clears the {t2}",
        f"the {t} affects the {t2} and nothing else this quarter",
        f"send the revised {t} and {a} will process it the same day",
        f"we are short two drivers so the {t} slips to {d}",
        f"once {a} signs the {t} the {t2} follows within a day",
    ])


def _a_sentence_exclaim_bang(rng: random.Random) -> str:
    """The exclaim builder with its mark attached - one mixer row, one shape."""
    return a_sentence_exclaim(rng) + "!"


# --------------------------------------------------------------------------
# Persona B slot machinery.
# --------------------------------------------------------------------------

B_HEDGE_LEAD: Final[tuple[str, ...]] = (
    "I would suggest that",
    "It would seem that",
    "I believe that",
    "I suspect that",
    "It may be that",
    "It might be that",
    "In my view",
    "I am inclined to think that",
    "I would think that",
    "Arguably",
    "Perhaps it is the case that",
    "One could argue that",
    "Possibly",
    "To some extent it appears that",
)

B_FORMAL_LEAD: Final[tuple[str, ...]] = (
    "Furthermore",
    "Moreover",
    "Nevertheless",
    "Accordingly",
    "Subsequently",
    "Therefore",
    "With respect to the matter at hand",
    "Regarding this point",
    "Concerning the schedule",
)

B_CLAUSE: Final[tuple[str, ...]] = (
    "we revisit the {t} prior to the renewal date",
    "the discrepancy on the {t} arises from a timing difference",
    "the {t} requires confirmation from {a} before we proceed",
    "we reconcile the {t} in advance of the quarterly review",
    "a brief call would resolve the {t} more efficiently",
    "the {t} should be circulated to all parties in writing",
    "our records concerning the {t} require reconciliation",
    "the revised {t} reflects the adjustments discussed previously",
    "{a} has requested clarification with respect to the {t}",
    "the {t} ought to be settled before the {t2} is finalised",
    "the amendment to the {t} warrants a separate conversation",
    "we document the {t} so that our records remain consistent",
    "the {t} has been forwarded to {a} for review",
    "the timing of the {t} presents somewhat of a difficulty",
)

B_TAIL: Final[tuple[str, ...]] = (
    "so that neither party is surprised by the adjustment",
    "in order to avoid any further delay on either side",
    "at your earliest convenience should that prove practicable",
    "prior to the close of the current billing cycle",
    "in the event that the schedule cannot accommodate it",
    "should you require additional documentation to that effect",
    "and I trust that this arrangement is agreeable to you",
    "though I defer entirely to your judgement on the matter",
    "provided that {a} is able to confirm the arrangement",
    "which would allow both organisations to conclude the review",
    "and I shall ensure the correspondence is filed accordingly",
    "so that the reporting expectations are established without ambiguity",
)


def _fill(tpl: str, rng: random.Random) -> str:
    """Fill a slot template's {t}/{t2}/{a} placeholders from the shared banks.

    WHY THREE DRAWS ALWAYS, even for a template using only ``{t}``:
    ``str.format`` evaluates its arguments before substituting, so all three
    ``rng.choice`` calls happen regardless of which placeholders appear.
    Making the draws conditional would be a harmless-looking optimisation
    that silently rewrites the entire corpus.
    """
    return tpl.format(t=rng.choice(THINGS), t2=rng.choice(THINGS), a=rng.choice(ACTORS))


def b_sentence_short(rng: random.Random) -> str:
    """Persona B, ~14-19 words - holds p25 near 17."""
    return rng.choice([
        _fill(
            "I trust this message finds you well and that the {t} has progressed on your side", rng
        ),
        _fill(
            "Thank you very much for your note regarding the {t} and the constraints you outlined",
            rng,
        ),
        _fill("I am grateful for your patience while {a} completed its review of the {t}", rng),
        _fill("Please be advised that the revised {t} is now with our contracts group", rng),
        _fill("I am writing regarding the {t} and the adjustments we discussed previously", rng),
        _fill("Accordingly I have asked {a} to reserve the {t} on a provisional basis", rng),
        _fill("Kindly confirm that the {t} remains acceptable to your team", rng),
        _fill("I should like to raise one further point concerning the {t}", rng),
    ])


def b_sentence_mid(rng: random.Random) -> str:
    """Persona B, ~22-27 words, composed: hedge lead + clause + tail."""
    lead = rng.choice(B_HEDGE_LEAD)
    clause = _fill(rng.choice(B_CLAUSE), rng)
    tail = _fill(rng.choice(B_TAIL), rng)
    return f"{lead} {clause} {tail}"


def b_sentence_long(rng: random.Random) -> str:
    """Persona B, ~30-38 words: formal lead + hedge + clause + clause + tail.

    The formal lead is drawn ONLY on the 65% arm - the draw order (hlead,
    c1, c2, tail, roll, then maybe flead) is part of the byte contract.
    """
    hlead = rng.choice(B_HEDGE_LEAD)
    c1 = _fill(rng.choice(B_CLAUSE), rng)
    c2 = _fill(rng.choice(B_CLAUSE), rng)
    tail = _fill(rng.choice(B_TAIL), rng)
    head = (
        f"{rng.choice(B_FORMAL_LEAD)} {hlead[0].lower() + hlead[1:]}"
        if rng.random() < 0.65
        else hlead
    )
    return f"{head} {c1}, and it follows that {c2} {tail}"


def b_sentence_closer(rng: random.Random) -> str:
    """Margaret's closing sentence - drawn whole, not composed.

    Closers are the one place a real formal writer repeats herself almost
    verbatim, so a fixed pool of four is the accurate shape here rather than
    the slot machinery the mid/long sentences use.
    """
    return rng.choice([
        _fill(
            "I trust that this arrangement is agreeable and I look forward to your confirmation regarding the {t}",
            rng,
        ),
        _fill(
            "Please do not hesitate to contact me should you require further clarification concerning the {t}",
            rng,
        ),
        _fill(
            "I am grateful for your continued attention to the {t} and shall await your response",
            rng,
        ),
        _fill(
            "I would be pleased to provide additional correspondence relating to the {t} upon request",
            rng,
        ),
    ])


# --------------------------------------------------------------------------
# The mixers. Cumulative thresholds against ONE rng draw; the row order and
# the numbers are both part of the determinism contract. The final threshold
# is +inf so the table is total and the walker needs no fallback branch.
# --------------------------------------------------------------------------

PERSONA_A_MIXER: Final[tuple[tuple[float, Callable[[random.Random], str]], ...]] = (
    (0.012, a_sentence_hedge),
    (0.030, a_sentence_formal),
    (0.048, _a_sentence_exclaim_bang),
    (0.230, a_sentence_contraction),
    (0.410, a_sentence_informal),
    (0.920, a_sentence_plain),
    (math.inf, a_sentence_long),
)
"""Dana's sentence-kind distribution, calibrated to her /100w marker rates."""

PERSONA_B_MIXER: Final[tuple[tuple[float, Callable[[random.Random], str]], ...]] = (
    (0.22, b_sentence_short),
    (0.66, b_sentence_mid),
    (0.92, b_sentence_long),
    (math.inf, b_sentence_closer),
)
"""Margaret's sentence-kind distribution. Same one-draw contract."""


def compose_sentence(
    rng: random.Random,
    mixer: tuple[tuple[float, Callable[[random.Random], str]], ...],
) -> str:
    """Draw ONE sentence kind from a mixer table and build it.

    Exactly one ``rng.random()`` selects the row; the chosen builder then
    makes its own draws. One draw, not a per-row loop of draws: the draw
    count is part of the determinism contract, so a "clearer" rewrite that
    consumes a different number of stream values would rewrite the corpus.
    """
    roll = rng.random()
    for threshold, build in mixer:
        if roll < threshold:
            return build(rng)
    # Unreachable: the final threshold is +inf. Raising keeps the walker
    # honest if a future edit truncates the table.
    raise ValueError("mixer table is not total - the final threshold must be +inf")
