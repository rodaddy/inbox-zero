"""The corpus-source seam: a profile document in, a VoiceProfile out.

Four properties are load-bearing and each has its own section below:

* the parser maps the PoC ``profile.md`` convention onto profile fields, and
  DROPS every blockquote -- the corpus quotes evidence, and a profile stores
  patterns, never quoted content;
* the ``#30`` marker convention survives the parse: an inferred greeting stays
  visibly inferred, an observed sign-off carries its observed text;
* a document that does not match the convention, or a field that would exceed
  ``profile.MAX_FIELD_LENGTH``, is REFUSED loudly rather than half-parsed or
  truncated;
* ``build_profile`` produces an UNAPPROVED profile, so the approval gate in
  ``profile`` is untouched by this path.

The last test builds from the REAL PoC corpus when it is present, read-only,
and skips when it is not -- the shipped kit does not carry ``pocs/``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_zero_connector.config import VoiceRouting
from inbox_zero_connector.services.voice.corpus import (
    CorpusDocumentError,
    ExternalCorpusDir,
    build_profile,
    parse_profile_document,
)
from inbox_zero_connector.services.voice.profile import MAX_FIELD_LENGTH, VoiceProfile

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

QUOTED_MARKER = "zzquotedevidencezz"

DOCUMENT = f"""# Voice profile: test-voice

Corpus: 3 episodes, 3435 s total (57.3 min), 1512 lines.
- `one.txt` -- 759 s; 349/390 lines kept.
- `two.txt` -- 1220 s; 509/620 lines kept.

Written corpus: 8 newsletter posts, 12,742 words, retrieved 2026-09-01.
- `war-on-the-young.md` -- 1855 words.

Confidence: Three episodes establish register with high confidence. What
neither corpus establishes is the PRIVATE CORRESPONDENCE voice.

## Register

Peer with more scar tissue, not teacher.

> [06:06] {QUOTED_MARKER} and I kept leasing big office space. -- `one.txt`

Profanity is baseline, not spike.

## Sentence habits

Short-to-medium sentences that land hard.

> [01:09] {QUOTED_MARKER} So, first off, ask yourself a question. -- `two.txt`

Repetition is immediate -- the same phrase twice, never a synonym.

## Lexicon

- `I think` -- 47. Dominant frame.

## How they explain

Datum first, opinion second -- never the reverse.

> [05:17] {QUOTED_MARKER} US valuations are stretched. -- `two.txt`

Analogies are physical and slightly absurd.

## Written register (observed + inferred)

- OBSERVED — Sign-off: `Life is so rich,` — invariant, 8 of 8 posts, as its
  own line after the final paragraph, with a comma and no name.
- OBSERVED — Paragraph length: median 97 words.
- INFERRED — Greeting in a one-to-one message: first name only, no
  comma-heavy formality.
- INFERRED — Length for a single-question reply: 150-350 words.

## Anti-patterns

Five things a draft in this voice must never do.
1. **Never open with rapport-building throat-clearing.** He reframes the
   question in one line and starts answering.
2. **Never assert a number without its source or its scale.** A bare
   percentage is off-voice.
3. **Never use motivational-business register.** Zero hits corpus-wide.
4. **Never soften a position into mush.** He hedges the FACT.
5. **Never give advice without the cost attached.** Advice that sounds free
   is off-voice.
"""


@pytest.fixture
def routing() -> VoiceRouting:
    """An installer that configured the voice these tests build."""
    return VoiceRouting(
        owner_domain="rtech-consulting.com",
        voices=("internal", "external", "sales", "test-voice"),
        sales_labels=("prospect",),
    )


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    """A directory holding a synthetic ``profile.md`` in the PoC convention."""
    (tmp_path / "profile.md").write_text(DOCUMENT, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Section mapping, and the blockquote drop
# ---------------------------------------------------------------------------


def test_parse_maps_every_section_to_its_field() -> None:
    fields = parse_profile_document(DOCUMENT)

    assert "scar tissue" in str(fields["formality"])
    assert "land hard" in str(fields["sentence_length"])
    assert "Datum first" in str(fields["structure"])
    assert "3435 s total" in str(fields["sample_summary"])
    assert "12,742 words" in str(fields["sample_summary"])
    assert "PRIVATE CORRESPONDENCE" in str(fields["unknowns"])


def test_parse_drops_every_blockquote_line() -> None:
    """profile.md QUOTES its evidence; a VoiceProfile stores patterns only."""
    fields = parse_profile_document(DOCUMENT)

    for name, value in fields.items():
        rendered = " ".join(value) if isinstance(value, tuple) else str(value)
        assert QUOTED_MARKER not in rendered, f"quoted evidence leaked into {name!r}"


def test_greeting_keeps_its_inferred_marker() -> None:
    """The #30 convention: a private-register guess stays visibly inferred."""
    fields = parse_profile_document(DOCUMENT)

    assert str(fields["greeting"]).startswith("INFERRED")
    assert "first name only" in str(fields["greeting"])


def test_sign_off_carries_the_observed_text() -> None:
    fields = parse_profile_document(DOCUMENT)

    assert "Life is so rich" in str(fields["sign_off"])
    assert not str(fields["sign_off"]).startswith("- OBSERVED")


def test_anti_patterns_land_in_never_does_without_bold_markers() -> None:
    fields = parse_profile_document(DOCUMENT)
    never_does = fields["never_does"]

    assert isinstance(never_does, tuple)
    assert len(never_does) == 5
    assert all("**" not in entry for entry in never_does)
    assert never_does[0].startswith("Never open with rapport-building")


def test_owner_corrections_is_never_produced_by_the_parser() -> None:
    """Owner-only field: the corpus cannot speak for what the owner said."""
    assert "owner_corrections" not in parse_profile_document(DOCUMENT)


# ---------------------------------------------------------------------------
# 2. Refusals
# ---------------------------------------------------------------------------


def test_an_over_cap_section_is_refused_by_name() -> None:
    filler = "\n".join(f"Line {index} of an over-long register section." for index in range(200))
    oversized = DOCUMENT.replace(
        "Peer with more scar tissue, not teacher.",
        "Peer with more scar tissue, not teacher.\n\n" + filler,
    )
    assert len(filler) > MAX_FIELD_LENGTH

    with pytest.raises(CorpusDocumentError, match="formality"):
        parse_profile_document(oversized)


def test_a_document_with_no_recognized_structure_is_refused() -> None:
    with pytest.raises(CorpusDocumentError, match="expected convention"):
        parse_profile_document("just some prose with no headings and no markers at all")


def test_external_corpus_dir_names_the_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CorpusDocumentError, match=r"profile\.md"):
        ExternalCorpusDir(tmp_path).profile_document()


def test_external_corpus_dir_refuses_an_empty_document(tmp_path: Path) -> None:
    (tmp_path / "profile.md").write_text("   \n\n  ", encoding="utf-8")

    with pytest.raises(CorpusDocumentError, match=r"profile\.md"):
        ExternalCorpusDir(tmp_path).profile_document()


# ---------------------------------------------------------------------------
# 3. build_profile leaves the approval gate alone
# ---------------------------------------------------------------------------


def test_build_profile_returns_an_unapproved_profile(
    corpus_dir: Path, routing: VoiceRouting
) -> None:
    built = build_profile(
        ExternalCorpusDir(corpus_dir), voice="Test-Voice", routing=routing, now=NOW
    )

    assert isinstance(built, VoiceProfile)
    assert built.voice == "test-voice"
    assert built.approved is False
    assert built.built_at == NOW.isoformat()


def test_build_profile_refuses_a_voice_the_installer_did_not_configure(
    corpus_dir: Path, routing: VoiceRouting
) -> None:
    with pytest.raises(ValueError, match="unknown voice"):
        build_profile(ExternalCorpusDir(corpus_dir), voice="marketing", routing=routing, now=NOW)


# ---------------------------------------------------------------------------
# 4. The real PoC corpus, read-only, skipped in the shipped kit
# ---------------------------------------------------------------------------


def _poc_corpus_dir() -> Path | None:
    """Walk up for the repo's ``pocs/voice-from-transcripts`` tree, or None."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pocs" / "voice-from-transcripts"
        if candidate.is_dir():
            return candidate / "voices" / "prof-g"
    return None


def test_build_profile_from_the_real_poc_corpus(routing: VoiceRouting) -> None:
    """The convention this parser targets is the one the PoC actually wrote."""
    corpus = _poc_corpus_dir()
    if corpus is None or not (corpus / "profile.md").is_file():
        pytest.skip("PoC corpus not present (shipped kit)")

    built = build_profile(ExternalCorpusDir(corpus), voice="test-voice", routing=routing, now=NOW)

    assert "Life is so rich" in built.sign_off
    assert built.greeting.startswith("INFERRED")
    assert len(built.never_does) == 5
    assert built.approved is False
    for _name, text in built._scannable():
        assert len(text) <= MAX_FIELD_LENGTH
