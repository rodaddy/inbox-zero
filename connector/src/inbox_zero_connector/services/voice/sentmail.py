"""Measuring sent mail WITHOUT carrying it -- the counting half of the corpus.

WHAT THIS IS
------------
``corpus`` names the ``CorpusSource`` contract and states that a sent-mail
source will implement it later. This module is the first half of that lane: it
turns a sample of the owner's real sent messages into COUNTS. It renders no
prose, implements no ``CorpusSource``, and decides nothing about whether a
sample is large enough to trust -- those belong to the lane that follows and are
deliberately absent here, so the measurement can be read and argued with on its
own terms.

THE SECURITY PROPERTY
---------------------
``SentStats`` carries counts, category names, and at most one recurrence-gated
short sign-off. It never carries a sentence the owner wrote. This is the whole
reason the measure/render split exists: the renderer downstream cannot leak what
the stats never held, so a confidential merger term or a client's name in a body
has no field to travel through. Everything that crosses out of a message here is
either a number, a member of a closed vocabulary (``SalutationForm.value``), or
a short closing that appeared in at least ``SIGN_OFF_MIN_COUNT`` DIFFERENT
messages and in at least ``SIGN_OFF_MIN_SHARE`` of the sample.

That recurrence gate is what makes the one text-carrying field safe. "Cheers,"
recurring across a third of the corpus is a habit; a line that appeared once is
content, and content is exactly what a one-off leak looks like. A single
message can never put its words into a ``SentStats``.

WHY BODIES ARE CLEANED FIRST
----------------------------
A real sent message is mostly not the owner's writing. It carries the quoted
thread below it, the reply header that introduces the quote, and a signature
block the mail client appended. Measuring sentence length over that measures the
CORRESPONDENT'S habits and the signature's line breaks, not the owner's. So
``clean_body`` strips all three before a single statistic is taken -- upstream of
the counting, not as a filter applied to results, because a statistic computed
over the wrong text cannot be corrected afterwards.

WHAT AN EMPTY SAMPLE MEANS
--------------------------
Zero substantive messages is a legitimate outcome, not an error: a mailbox of
one-line acknowledgements is a real mailbox. ``measure_sent_messages`` returns
zeroed stats with ``total_count`` preserved, so the caller can see the sample was
read and found thin. Whether thin is USABLE is the next lane's judgment, made
against a floor this module does not own.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from loguru import logger
from pydantic import BaseModel, ConfigDict

from inbox_zero_connector.providers.base import MailProvider, SentMessage, SentSelection
from inbox_zero_connector.services.voice.corpus import CorpusDocumentError
from inbox_zero_connector.services.voice.drafting import classify_inbound_salutation

MIN_SUBSTANTIVE_WORDS: Final = 5
"""Below this a cleaned body is an acknowledgement, not a sample of the voice.

"Sounds good, thanks" tells a reader nothing about sentence habits and would
drag every average toward one-line replies, which is the shape of a mailbox
rather than the shape of a writer.
"""

SIGN_OFF_MAX_WORDS: Final = 6
"""Past this the last line is a closing SENTENCE, not a sign-off.

The bound is what keeps the one text-carrying field from becoming a content
field: "See you at the meeting tomorrow morning" is a thing the owner said to
one person, and it must never be eligible to become a stored candidate.
"""

SIGN_OFF_MIN_SHARE: Final = 0.30
"""Fraction of substantive messages a closing must cover to count as a habit."""

SIGN_OFF_MIN_COUNT: Final = 3
"""And the absolute floor, so a 2-message sample cannot make one line a habit.

Share alone is not enough: one closing in a 2-message corpus is a 50% share and
is still a single sentence from a single message. Both gates must pass.
"""

MIN_SUBSTANTIVE_MESSAGES: Final = 10
"""The thin-mailbox floor.

The volume-curve receipts on #28 showed discrimination held at N=10 full-band,
and any tighter number is a tolerance choice -- a declared product knob, not a
measurement claim.
"""

GREETING_OBSERVED_MIN: Final = 5
"""One-to-one messages needed before a greeting is marked OBSERVED.

Below it the salutation counts are real but too few to call a habit, so the
document says INFERRED and states the count it is inferring from.
"""

_REPLY_HEADER = re.compile(r"^On .{5,200} wrote:$")
"""The line a client writes above quoted history. Everything below it is theirs."""

_QUOTED_LINE = re.compile(r"^>")
_SIGNATURE_DELIMITER: Final = {"--", "-- "}
"""RFC 3676's signature separator, in both the spec spelling and the common one."""

_BLANK_RUN = re.compile(r"\n{3,}")
_SENTENCE_SPLIT = re.compile(r"([.!?]+)(?=\s|$)")
_LIST_LINE = re.compile(r"^\s*([-*•]|\d+[.)])\s+")
_CONTRACTION = re.compile(r"\b\w+'(s|t|re|ve|ll|d|m)\b")
_FORMAL_MARKER = re.compile(r"\b(Sincerely|Regards|Respectfully)\b")
_SIGN_OFF_PUNCTUATION: Final = " .,!;:"


class SentStats(BaseModel):
    """Everything measurement is allowed to hand onward. Frozen by intent.

    Frozen because these numbers are a MEASUREMENT of a fixed sample: a caller
    that could adjust a field after the fact could launder an unmeasured claim
    into something that looks measured, and the renderer downstream has no way
    to tell the difference.

    Read the field list as the security boundary it is. Every field is a number
    or a count keyed by a closed vocabulary, except ``sign_off_candidate``, which
    is gated by recurrence, and ``first_sent``/``last_sent``, which are
    timestamps. No field can hold a sentence.
    """

    model_config = ConfigDict(frozen=True)

    message_count: int
    """Substantive messages actually measured."""

    total_count: int
    """Messages handed in before the substantive filter -- how thin the sample was."""

    first_sent: str = ""
    last_sent: str = ""
    median_words_per_sentence: float = 0.0
    mean_sentences_per_message: float = 0.0
    mean_paragraphs_per_message: float = 0.0
    list_usage_rate: float = 0.0
    contractions_per_100_words: float = 0.0
    exclamations_per_100_sentences: float = 0.0
    questions_per_100_sentences: float = 0.0
    formal_markers_per_100_messages: float = 0.0
    one_to_one_count: int = 0

    salutation_forms: dict[str, int] = {}
    """``SalutationForm.value`` -> count, over one-to-one messages only.

    One-to-one only because a greeting to a distribution list is addressed to a
    role, not a person, and mirroring off it would teach the profile a register
    the owner only uses for announcements.
    """

    sign_off_candidate: str = ""
    """The owner's habitual closing, or ``""`` when no closing recurs enough."""

    sign_off_share: float = 0.0
    """The top closing group's share, REPORTED even when the gate refused it.

    Reported unconditionally so a caller can see the difference between "no
    closings at all" and "closings, all different" -- which is the difference
    between a mailbox with no habit and a sample too small to have found one.
    """


def clean_body(body: str) -> str:
    """Strip everything in a sent body that the owner did not write here.

    Applied in order, because each step can expose the next: a reply header
    truncation removes the quoted block wholesale, and quote removal can leave
    the signature delimiter as the first surviving marker.

    Args:
        body: The raw sent-message body.

    Returns:
        The owner's own text for this message, with blank runs collapsed.
    """
    kept: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if _REPLY_HEADER.match(stripped) or line in _SIGNATURE_DELIMITER:
            break
        if _QUOTED_LINE.match(line):
            continue
        kept.append(line)
    return _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()


def _sentences(text: str) -> list[tuple[str, str]]:
    """Split cleaned text into ``(sentence, terminator)`` pairs.

    The terminator run is returned alongside the sentence because exclamation
    and question rates are counted off it -- "Really?!" is one sentence that is
    both, and dropping the run would lose that.
    """
    parts = _SENTENCE_SPLIT.split(text)
    pairs: list[tuple[str, str]] = []
    for index in range(0, len(parts) - 1, 2):
        body, terminator = parts[index].strip(), parts[index + 1]
        if body:
            pairs.append((body, terminator))
    tail = parts[-1].strip() if len(parts) % 2 else ""
    if tail:
        pairs.append((tail, ""))
    return pairs


def _sign_off_key(line: str) -> str:
    """Normalise a closing to its grouping key: lowercased, punctuation stripped."""
    return line.strip(_SIGN_OFF_PUNCTUATION).lower()


@dataclass
class _Tally:
    """Running totals across the sample.

    A mutable accumulator rather than a dozen locals threaded through a loop:
    the per-message counting and the final division are two different jobs, and
    splitting them is what keeps either one readable at a glance.
    """

    sentence_word_counts: list[int] = field(default_factory=list)
    sentences: int = 0
    paragraphs: int = 0
    words: int = 0
    contractions: int = 0
    exclamations: int = 0
    questions: int = 0
    list_messages: int = 0
    formal_messages: int = 0
    one_to_one: int = 0
    salutations: Counter[str] = field(default_factory=Counter)
    sign_off_groups: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add(self, message: SentMessage, text: str) -> None:
        """Fold one cleaned substantive message into the totals."""
        for sentence, terminator in _sentences(text):
            self.sentences += 1
            self.sentence_word_counts.append(len(sentence.split()))
            self.exclamations += "!" in terminator
            self.questions += "?" in terminator

        self.paragraphs += len([block for block in text.split("\n\n") if block.strip()])
        self.words += len(text.split())
        self.contractions += len(_CONTRACTION.findall(text))

        lines = [line for line in text.splitlines() if line.strip()]
        if any(_LIST_LINE.match(line) for line in lines):
            self.list_messages += 1
        if text.startswith("Dear ") or _FORMAL_MARKER.search(text):
            self.formal_messages += 1
        if len(message.recipients) == 1:
            self.one_to_one += 1
            self.salutations[classify_inbound_salutation(text).value] += 1
        if lines and len(lines[-1].strip().split()) <= SIGN_OFF_MAX_WORDS:
            last = lines[-1].strip()
            self.sign_off_groups[_sign_off_key(last)].append(last)

    def sign_off(self, count: int) -> tuple[str, float]:
        """The habitual closing and its share, or ``("", share)`` when gated out."""
        if not self.sign_off_groups:
            return "", 0.0
        top = max(self.sign_off_groups.values(), key=len)
        share = len(top) / count
        if len(top) >= SIGN_OFF_MIN_COUNT and share >= SIGN_OFF_MIN_SHARE:
            return Counter(top).most_common(1)[0][0], share
        return "", share


def measure_sent_messages(messages: Sequence[SentMessage]) -> SentStats:
    """Count the owner's writing habits across a sample of sent mail.

    PURE and deterministic -- no model, no I/O, stdlib and regex only. Every
    statistic is taken over CLEANED bodies of substantive messages; the sample's
    unfiltered size survives as ``total_count`` so a caller can see what was
    discarded.

    Args:
        messages: The explicitly selected sent-mail sample. May be empty.

    Returns:
        Counts only. No sentence from any message reaches a field -- see the
        module docstring for why that is the point rather than a side effect.
    """
    cleaned: list[tuple[SentMessage, str]] = []
    for message in messages:
        text = clean_body(message.body)
        if len(text.split()) >= MIN_SUBSTANTIVE_WORDS:
            cleaned.append((message, text))

    if not cleaned:
        logger.debug(
            "sent-mail measurement found no substantive messages in {} given", len(messages)
        )
        return SentStats(message_count=0, total_count=len(messages))

    count = len(cleaned)
    tally = _Tally()
    for message, text in cleaned:
        tally.add(message, text)

    sign_off_candidate, sign_off_share = tally.sign_off(count)
    sent_dates = sorted(message.sent for message, _ in cleaned)
    per_100_sentences = 100 / tally.sentences if tally.sentences else 0.0

    return SentStats(
        message_count=count,
        total_count=len(messages),
        first_sent=sent_dates[0],
        last_sent=sent_dates[-1],
        median_words_per_sentence=(
            float(statistics.median(tally.sentence_word_counts))
            if tally.sentence_word_counts
            else 0.0
        ),
        mean_sentences_per_message=tally.sentences / count,
        mean_paragraphs_per_message=tally.paragraphs / count,
        list_usage_rate=tally.list_messages / count,
        contractions_per_100_words=(tally.contractions / tally.words * 100 if tally.words else 0.0),
        exclamations_per_100_sentences=tally.exclamations * per_100_sentences,
        questions_per_100_sentences=tally.questions * per_100_sentences,
        formal_markers_per_100_messages=tally.formal_messages / count * 100,
        one_to_one_count=tally.one_to_one,
        salutation_forms=dict(tally.salutations),
        sign_off_candidate=sign_off_candidate,
        sign_off_share=sign_off_share,
    )


def _register_section(stats: SentStats) -> list[str]:
    """Banded prose for ``## Register``, off contractions and formal markers."""
    rate = stats.contractions_per_100_words
    if rate >= 4:
        lines = [f"Writes informally: contractions in running use ({rate:.1f} per 100 words)."]
    elif rate >= 1:
        lines = [
            "Writes in a mixed register: contractions appear but are not the default "
            f"({rate:.1f} per 100 words)."
        ]
    else:
        lines = [f"Writes formally: contractions are rare ({rate:.1f} per 100 words)."]
    if stats.formal_markers_per_100_messages >= 50:
        lines.append(
            "Formal openings and sign-off markers are habitual "
            f"({stats.formal_markers_per_100_messages:.1f} per 100 messages)."
        )
    return lines


def _sentence_section(stats: SentStats) -> list[str]:
    """Banded prose for ``## Sentence habits``, off median length and volume."""
    median = stats.median_words_per_sentence
    if median < 12:
        opener = f"Short sentences ({median:.1f} words at the median)."
    elif median <= 20:
        opener = f"Moderate sentence length ({median:.1f} words at the median)."
    else:
        opener = f"Long sentences ({median:.1f} words at the median)."
    return [
        opener,
        f"Messages run {stats.mean_sentences_per_message:.1f} sentences on average.",
    ]


def _structure_section(stats: SentStats) -> list[str]:
    """Banded prose for ``## How they explain``, off paragraphing and lists."""
    mean = stats.mean_paragraphs_per_message
    if mean <= 1.5:
        lines = [f"Explains in a single block ({mean:.1f} paragraphs per message)."]
    else:
        lines = [f"Explains across paragraphs ({mean:.1f} per message)."]
    if stats.list_usage_rate >= 0.25:
        lines.append(
            "Reaches for lists when there is more than one thing to say "
            f"({stats.list_usage_rate:.0%} of messages)."
        )
    return lines


def _anti_patterns(stats: SentStats) -> list[str]:
    """ZERO-COUNT absences only, each stating the basis it was read off.

    An absence is the one thing a count can assert as a rule: "never" over a
    measured sample is a fact about the sample, where "rarely" is a judgment
    about how rare is rare. So a nonzero rate produces no line at all.
    """
    count = stats.message_count
    items: list[str] = []
    if stats.exclamations_per_100_sentences == 0:
        items.append(f"Never uses exclamation marks (0 in {count} messages).")
    if stats.questions_per_100_sentences == 0:
        items.append(f"Never asks questions (0 in {count} messages).")
    if stats.formal_markers_per_100_messages == 0:
        items.append(
            "Never opens with 'Dear' or closes with formal sign-off markers "
            f"(0 in {count} messages)."
        )
    return items


def _marked_lines(stats: SentStats) -> list[str]:
    """The written-register lines, each carrying the marker its evidence earns.

    Sign-off is OBSERVED or absent: ``measure_sent_messages``'s recurrence gate
    is what licenses the claim, so an ungated candidate is never rendered at
    all. Greeting is the axis a sent-mail corpus can observe where a broadcast
    corpus cannot -- but only above ``GREETING_OBSERVED_MIN``; under it the
    counts are real and the habit is not yet established, which is exactly what
    INFERRED means.
    """
    lines: list[str] = []
    if stats.sign_off_candidate:
        lines.append(f"- OBSERVED — Sign-off: {stats.sign_off_candidate}")
    if stats.one_to_one_count and stats.salutation_forms:
        form, count = max(stats.salutation_forms.items(), key=lambda pair: pair[1])
        marker = "OBSERVED" if stats.one_to_one_count >= GREETING_OBSERVED_MIN else "INFERRED"
        lines.append(
            f"- {marker} — Greeting: opens one-to-one mail with a {form} salutation "
            f"({count}/{stats.one_to_one_count} messages)."
        )
    return lines


def _confidence(stats: SentStats) -> str:
    """What sent mail cannot show -- always the register, plus thin axes."""
    limits = [
        "spoken register, which sent mail cannot show at all",
    ]
    if stats.one_to_one_count == 0:
        limits.append("greeting form -- no salutation data, since no one-to-one mail was selected")
    if not stats.sign_off_candidate:
        limits.append("sign-off form, which varied rather than recurring")
    return "Confidence: measured from sent mail only. Not established: " + "; ".join(limits) + "."


def render_profile_document(stats: SentStats) -> str:
    """Render measured stats into the ``profile.md`` convention. Pure; no I/O.

    Every sentence here comes from a fixed table keyed on a NUMBER. That is the
    whole security argument and it is structural rather than careful: a
    ``SentStats`` carries no sentence the owner wrote, so there is no field for
    a merger term or a client's name to travel through, and no template
    interpolates one. The document describes the writing; it never quotes it.

    Args:
        stats: Counts from ``measure_sent_messages``.

    Returns:
        A profile document ``corpus.parse_profile_document`` ingests.
    """
    blocks: list[str] = [
        f"# Voice profile from sent mail\n\n"
        f"Corpus: {stats.message_count} sent messages, {stats.first_sent}..{stats.last_sent}, "
        "provider sent folder, measured deterministically.",
        _confidence(stats),
        "## Register\n\n" + "\n".join(_register_section(stats)),
        "## Sentence habits\n\n" + "\n".join(_sentence_section(stats)),
        "## How they explain\n\n" + "\n".join(_structure_section(stats)),
    ]
    absences = _anti_patterns(stats)
    if absences:
        numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(absences, start=1))
        blocks.append("## Anti-patterns\n\n" + numbered)
    marked = _marked_lines(stats)
    if marked:
        blocks.append("## Written register\n\n" + "\n".join(marked))
    return "\n\n".join(blocks) + "\n"


class SentMailCorpus:
    """A ``CorpusSource`` over the owner's own sent mail.

    The other side of ``corpus.ExternalCorpusDir``: where that reads a document
    somebody authored, this MEASURES the mailbox and renders one. Both hand the
    caller the same convention, which is why the caller's choice between them is
    a two-line ``if`` rather than a strategy.

    Holds the provider and the selection and reads NOTHING until
    ``profile_document`` -- constructing a corpus is not permission to open the
    mailbox, and the selection is the security boundary the read happens inside.
    """

    def __init__(self, provider: MailProvider, selection: SentSelection) -> None:
        """Bind the provider and the explicit selection. Reads nothing yet.

        Args:
            provider: The mailbox to sample; ``read_sent_sample`` is the one
                method a voice build is allowed to reach.
            selection: The EXPLICIT sent-mail selection. Never widened here.
        """
        self.provider = provider
        self.selection = selection

    def profile_document(self) -> str:
        """Measure the selected sample and render its profile document.

        Returns:
            The rendered document.

        Raises:
            CorpusDocumentError: If fewer than ``MIN_SUBSTANTIVE_MESSAGES``
                substantive messages were found. The refusal is loud and names
                the recovery for the same reason the provider's empty-selection
                refusal does: a profile quietly built from four acknowledgements
                would be approved by an owner who had no way to see it was thin.
        """
        messages = self.provider.read_sent_sample(self.selection)
        stats = measure_sent_messages(messages)
        if stats.message_count < MIN_SUBSTANTIVE_MESSAGES:
            raise CorpusDocumentError(
                f"this sent-mail sample holds {stats.message_count} substantive messages "
                f"({stats.total_count} read), under the {MIN_SUBSTANTIVE_MESSAGES}-message "
                "floor a measured voice profile needs. Widen the sent-mail selection, or "
                "build from an external corpus directory instead."
            )
        logger.info(
            f"VOICE: sent-mail corpus rendered message_count={stats.message_count} "
            f"total_count={stats.total_count}"
        )
        return render_profile_document(stats)
