"""The sent-mail RENDER half: counts in, a profile document out.

The measure half proved a ``SentStats`` cannot hold a sentence. This file
proves the consequence: everything the renderer emits comes from fixed tables
keyed on numbers, so a fixture body sentence has no route into the document --
and the document it emits round-trips through ``corpus.parse_profile_document``,
which is the only reason rendering into that convention is worth doing at all.

The marker tests are the other load-bearing half. A sent-mail corpus can
OBSERVE a one-to-one greeting where a broadcast corpus structurally cannot, so
OBSERVED is now a legal greeting marker -- but only above a stated floor, and
the parser must keep whichever marker the document carried.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inbox_zero_connector.config import VoiceRouting
from inbox_zero_connector.providers.base import SentMessage, SentSelection
from inbox_zero_connector.services.voice import corpus
from inbox_zero_connector.services.voice.sentmail import (
    GREETING_OBSERVED_MIN,
    MIN_SUBSTANTIVE_MESSAGES,
    SentMailCorpus,
    SentStats,
    render_profile_document,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

FIXTURE_SENTENCES = (
    "The Hendricks merger closes on the twelfth of March",
    "I told Karen the escrow number was wrong",
    "Draft the indemnity clause before Thursday",
)

BODY = (
    "Hi Bob,\n\n"
    f"{FIXTURE_SENTENCES[0]}. {FIXTURE_SENTENCES[1]}.\n\n"
    f"{FIXTURE_SENTENCES[2]}. We can review it together after that.\n\n"
    "Thanks,"
)


def _msg(index: int, *, recipients: list[str] | None = None) -> SentMessage:
    """One substantive sent message; only the recipient count varies."""
    return SentMessage(
        id=f"m{index}",
        thread_id=f"t{index}",
        recipients=["bob@example.com"] if recipients is None else recipients,
        subject="s",
        sent=f"2026-01-{index + 1:02d}",
        body=BODY,
    )


def _stats(**overrides: object) -> SentStats:
    """A rich ``SentStats`` every band of the renderer can be steered off."""
    base: dict[str, object] = {
        "message_count": 40,
        "total_count": 52,
        "first_sent": "2026-01-02",
        "last_sent": "2026-06-30",
        "median_words_per_sentence": 14.0,
        "mean_sentences_per_message": 5.5,
        "mean_paragraphs_per_message": 2.4,
        "list_usage_rate": 0.35,
        "contractions_per_100_words": 5.2,
        "exclamations_per_100_sentences": 1.5,
        "questions_per_100_sentences": 3.0,
        "formal_markers_per_100_messages": 10.0,
        "one_to_one_count": 30,
        "salutation_forms": {"first_name": 22, "hi_only": 8},
        "sign_off_candidate": "Thanks,",
        "sign_off_share": 0.62,
    }
    return SentStats(**(base | overrides))  # type: ignore[arg-type]


@pytest.fixture
def routing() -> VoiceRouting:
    """An installer that configured the voice these tests build."""
    return VoiceRouting(
        owner_domain="rtech-consulting.com",
        voices=("internal", "external", "sales", "test-voice"),
        sales_labels=("prospect",),
    )


@pytest.fixture
def selection() -> SentSelection:
    """A bounded, non-empty sent selection."""
    return SentSelection(labels=("sent",), after="2026-01-01")


class FakeProvider:
    """A provider stub that returns a fixed sent sample and records the ask."""

    def __init__(self, messages: list[SentMessage]) -> None:
        self.messages = messages
        self.seen: list[SentSelection] = []

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        self.seen.append(selection)
        return list(self.messages)


# ---------------------------------------------------------------------------
# 1. The rendered document round-trips through the parser
# ---------------------------------------------------------------------------


def test_rendered_document_parses_into_profile_fields() -> None:
    fields = corpus.parse_profile_document(render_profile_document(_stats()))

    assert "40" in str(fields["sample_summary"])
    assert "2026-06-30" in str(fields["sample_summary"])
    assert str(fields["unknowns"]).lower().startswith("confidence:")
    assert "contraction" in str(fields["formality"]).lower()
    assert "14" in str(fields["sentence_length"])
    assert "paragraph" in str(fields["structure"]).lower()


def test_register_bands_state_their_measured_numbers() -> None:
    informal = render_profile_document(_stats(contractions_per_100_words=6.0))
    formal = render_profile_document(
        _stats(contractions_per_100_words=0.4, formal_markers_per_100_messages=70.0)
    )

    assert "informal" in informal.lower()
    assert "6.0" in informal
    assert "formal" in formal.lower()
    assert "70.0" in formal


def test_source_document_is_the_seam_the_corpus_module_accepts() -> None:
    assert isinstance(SentMailCorpus(FakeProvider([]), SentSelection()), corpus.CorpusSource)


# ---------------------------------------------------------------------------
# 2. Anti-patterns are zero-count absences only
# ---------------------------------------------------------------------------


def test_zero_count_absences_become_never_does() -> None:
    fields = corpus.parse_profile_document(
        render_profile_document(
            _stats(
                exclamations_per_100_sentences=0.0,
                questions_per_100_sentences=0.0,
                formal_markers_per_100_messages=0.0,
            )
        )
    )

    never = " ".join(str(item) for item in fields["never_does"])
    assert "exclamation" in never.lower()
    assert "question" in never.lower()
    assert "Dear" in never


def test_a_nonzero_rate_produces_no_anti_pattern_line() -> None:
    document = render_profile_document(_stats(exclamations_per_100_sentences=2.0))

    assert "exclamation mark" not in document.lower()


def test_anti_patterns_section_is_omitted_when_nothing_is_absent() -> None:
    document = render_profile_document(_stats())

    assert "## Anti-patterns" not in document
    assert "never_does" not in corpus.parse_profile_document(document)


# ---------------------------------------------------------------------------
# 3. The written-register marked lines
# ---------------------------------------------------------------------------


def test_observed_sign_off_line_appears_only_with_a_candidate() -> None:
    with_candidate = corpus.parse_profile_document(render_profile_document(_stats()))
    without = corpus.parse_profile_document(render_profile_document(_stats(sign_off_candidate="")))

    assert with_candidate["sign_off"] == "Thanks,"
    assert "sign_off" not in without


@pytest.mark.parametrize(
    ("one_to_one", "marker"),
    [(30, "OBSERVED"), (GREETING_OBSERVED_MIN - 1, "INFERRED")],
)
def test_greeting_marker_follows_the_one_to_one_count(one_to_one: int, marker: str) -> None:
    fields = corpus.parse_profile_document(
        render_profile_document(_stats(one_to_one_count=one_to_one))
    )

    greeting = str(fields["greeting"])
    assert greeting.startswith(marker)
    assert "first_name" in greeting
    assert str(one_to_one) in greeting


def test_no_greeting_line_without_one_to_one_mail() -> None:
    document = render_profile_document(_stats(one_to_one_count=0, salutation_forms={}))

    assert "Greeting" not in document
    assert "greeting" not in corpus.parse_profile_document(document)
    assert "no salutation data" in document.lower()


# ---------------------------------------------------------------------------
# 4. End to end, through the CorpusSource seam
# ---------------------------------------------------------------------------


def test_build_profile_from_a_sent_sample(routing: VoiceRouting, selection: SentSelection) -> None:
    provider = FakeProvider([_msg(index) for index in range(12)])

    built = corpus.build_profile(
        SentMailCorpus(provider, selection), voice="test-voice", routing=routing, now=NOW
    )

    assert built.voice == "test-voice"
    assert built.approved is False
    assert provider.seen == [selection]


def test_a_thin_mailbox_is_refused_with_its_count_and_the_floor(
    selection: SentSelection,
) -> None:
    thin = SentMailCorpus(
        FakeProvider([_msg(i) for i in range(MIN_SUBSTANTIVE_MESSAGES - 1)]), selection
    )

    with pytest.raises(corpus.CorpusDocumentError) as error:
        thin.profile_document()

    message = str(error.value)
    assert str(MIN_SUBSTANTIVE_MESSAGES - 1) in message
    assert str(MIN_SUBSTANTIVE_MESSAGES) in message
    assert "external corpus" in message


def test_the_floor_itself_builds(selection: SentSelection) -> None:
    at_floor = SentMailCorpus(
        FakeProvider([_msg(i) for i in range(MIN_SUBSTANTIVE_MESSAGES)]), selection
    )

    assert "Corpus:" in at_floor.profile_document()


# ---------------------------------------------------------------------------
# 5. The security property: no message text reaches the document
# ---------------------------------------------------------------------------


def test_no_fixture_sentence_survives_into_the_rendered_document(
    selection: SentSelection,
) -> None:
    document = SentMailCorpus(
        FakeProvider([_msg(index) for index in range(12)]), selection
    ).profile_document()

    for sentence in FIXTURE_SENTENCES:
        assert sentence not in document
        assert sentence.lower() not in document.lower()


# ---------------------------------------------------------------------------
# 6. Regression: the corpus widening keeps INFERRED documents identical
# ---------------------------------------------------------------------------


def test_an_inferred_greeting_document_still_parses_unchanged() -> None:
    document = (
        "Corpus: 8 posts.\n\n"
        "## Register\n\nPeer, not teacher.\n\n"
        "- INFERRED — Greeting in a one-to-one message: first name only.\n"
    )

    fields = corpus.parse_profile_document(document)

    assert str(fields["greeting"]).startswith("INFERRED — Greeting in a one-to-one message")
