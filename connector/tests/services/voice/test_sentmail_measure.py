"""What the sent-mail MEASURE half is allowed to know, and what it must not carry.

Two kinds of test here. The arithmetic ones pin hand-computed values so a
refactor that changes a denominator fails loudly rather than drifting silently.
The last one is the security test: it walks every field of a built ``SentStats``
and asserts no fixture sentence survives into it. That is the property the
renderer downstream depends on -- it cannot leak what the stats never held.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.providers.base import SentMessage
from inbox_zero_connector.services.voice.sentmail import (
    SIGN_OFF_MIN_COUNT,
    SentStats,
    clean_body,
    measure_sent_messages,
)


def _msg(
    body: str,
    *,
    recipients: list[str] | None = None,
    sent: str = "2026-01-01",
) -> SentMessage:
    """One sent message carrying only the fields these tests vary."""
    return SentMessage(
        id="m1",
        thread_id="t1",
        recipients=["bob@example.com"] if recipients is None else recipients,
        subject="s",
        sent=sent,
        body=body,
    )


def test_clean_body_drops_quoted_lines() -> None:
    cleaned = clean_body("My answer is yes.\n> your original question\n> second quoted line")
    assert cleaned == "My answer is yes."


def test_clean_body_truncates_at_reply_header() -> None:
    body = "Short reply here.\n\nOn Mon, Jan 1, 2026 at 9:00 AM Bob Smith wrote:\nolder content"
    assert clean_body(body) == "Short reply here."


def test_clean_body_truncates_at_signature_delimiter() -> None:
    body = "The real message.\n--\nRico Rojas\nrTech Consulting"
    assert clean_body(body) == "The real message."


def test_clean_body_collapses_long_blank_runs() -> None:
    assert clean_body("one\n\n\n\n\ntwo") == "one\n\ntwo"


def test_short_message_is_counted_but_not_measured() -> None:
    stats = measure_sent_messages([_msg("Thanks so much Bob")])
    assert stats.total_count == 1
    assert stats.message_count == 0


def test_median_words_and_contraction_rate_are_exact() -> None:
    # Sentences of 4, 6, and 8 words -> median 6.0.
    # 18 words total, 2 contractions -> 2/18*100.
    body = (
        "I can't do that. You won't need the extra thing. "
        "Here is a longer sentence with eight words."
    )
    stats = measure_sent_messages([_msg(body)])
    assert stats.message_count == 1
    assert stats.median_words_per_sentence == pytest.approx(6.0)
    assert stats.contractions_per_100_words == pytest.approx(2 / 18 * 100)


def test_salutation_forms_count_one_to_one_only() -> None:
    one = _msg("Hi Bob,\nHere is the thing you asked me about yesterday.")
    many = _msg(
        "Hi Team,\nHere is the thing you asked me about yesterday.",
        recipients=["a@x.com", "b@x.com"],
    )
    stats = measure_sent_messages([one, many])
    assert stats.message_count == 2
    assert stats.one_to_one_count == 1
    assert stats.salutation_forms == {"hi": 1}


def test_sign_off_candidate_set_when_recurrence_gate_passes() -> None:
    signed = "Here is the update you asked me for.\nCheers,"
    other = "Here is the update you asked me for.\nSee you at the meeting tomorrow morning."
    messages = [_msg(signed) for _ in range(4)] + [_msg(other) for _ in range(6)]
    stats = measure_sent_messages(messages)
    assert stats.message_count == 10
    assert stats.sign_off_candidate == "Cheers,"
    assert stats.sign_off_share == pytest.approx(0.4)


def test_sign_off_candidate_empty_when_all_closings_differ() -> None:
    closings = ["Cheers,", "Best,", "Thanks,", "Later,", "Ciao,"]
    messages = [_msg(f"Here is the update you asked me for.\n{c}") for c in closings]
    stats = measure_sent_messages(messages)
    assert stats.sign_off_candidate == ""
    assert stats.sign_off_share == pytest.approx(1 / len(closings))
    assert SIGN_OFF_MIN_COUNT > 1


def test_long_last_line_is_never_a_sign_off_candidate() -> None:
    body = "Here is the update.\nThis final line has far more than six words in it."
    stats = measure_sent_messages([_msg(body) for _ in range(10)])
    assert stats.sign_off_candidate == ""
    assert stats.sign_off_share == pytest.approx(0.0)


def test_zero_messages_returns_empty_stats() -> None:
    stats = measure_sent_messages([])
    assert stats == SentStats(message_count=0, total_count=0)


def test_zero_substantive_messages_does_not_raise() -> None:
    stats = measure_sent_messages([_msg("ok"), _msg("sure thing")])
    assert stats.message_count == 0
    assert stats.total_count == 2
    assert stats.median_words_per_sentence == pytest.approx(0.0)


def test_first_and_last_sent_bound_substantive_messages() -> None:
    stats = measure_sent_messages([
        _msg("A properly long sentence for measurement.", sent="2026-03-04"),
        _msg("Another properly long sentence for measurement.", sent="2026-01-02"),
    ])
    assert stats.first_sent == "2026-01-02"
    assert stats.last_sent == "2026-03-04"


def test_list_usage_and_formal_markers() -> None:
    listy = _msg("Here is the plan for next week:\n- first item\n- second item")
    formal = _msg("Dear Bob,\nThe report is attached for your review.\nSincerely,")
    stats = measure_sent_messages([listy, formal])
    assert stats.list_usage_rate == pytest.approx(0.5)
    assert stats.formal_markers_per_100_messages == pytest.approx(50.0)


def test_no_stats_field_carries_a_fixture_sentence() -> None:
    sentences = [
        "The merger documents are with opposing counsel today.",
        "Our client will not accept the revised indemnity clause.",
        "I cannot send the settlement figure before Thursday.",
    ]
    messages = [_msg(f"Hi Bob,\n{s}\nCheers,") for s in sentences for _ in range(4)]
    stats = measure_sent_messages(messages)
    rendered = repr(stats.model_dump())
    for sentence in sentences:
        assert sentence not in rendered
        assert sentence.rstrip(".") not in rendered
