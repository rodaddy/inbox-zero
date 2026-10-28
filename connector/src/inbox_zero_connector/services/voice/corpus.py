"""Where a voice profile's PROSE comes from -- a seam with two sides, one shape.

WHAT THIS IS
------------
``profile`` stores a voice profile and refuses a disclosing one. It does not say
where the description came from, and deliberately so: it is handed the derived
text, never the mail. This module is the other half of that sentence -- the
SOURCE of that text -- and it names exactly one contract for it.

A ``CorpusSource`` yields one thing: a profile DOCUMENT, in the convention the
``voice-from-transcripts`` PoC established (``pocs/voice-from-transcripts/
voices/<voice>/profile.md``). Markdown, a preamble stating the corpus and the
confidence, ``## ``-headed sections for register / sentence habits / how they
explain / anti-patterns, and ``- OBSERVED --`` / ``- INFERRED --`` marked lines
for the written register. The convention is not invented here; it is READ here,
because the PoC already proved it carries what a profile needs.

THE CALLER CHOOSES, NOT THIS MODULE
-----------------------------------
Two sources will exist. ``ExternalCorpusDir`` is here now: a directory of
material authored OUTSIDE the mailbox -- published writing, transcripts, an
onboarding questionnaire -- which is what a new owner has on day one, before
there is any sent mail worth reading. A ``SentMailCorpus`` will implement the
same ``Protocol`` later, over the sample ``providers.base.read_sent_sample``
returns.

Selection between them is the CALLER'S deterministic choice, made the same way
``config.resolve_voice`` makes its choice -- from facts, not preference: the
sent-mail corpus when it exists, the external corpus when it does not. There is
no scoring, no blend, and no place for a model to express an opinion about which
description of the owner it prefers. One Protocol with one method is what makes
that choice a two-line ``if`` at the call site instead of a strategy object.

The measured-to-prose translation a ``SentMailCorpus`` needs -- turning counted
observations over real messages into the same document convention, without the
counts becoming quoted content -- is issue #34/#81's open design and is NOT
decided here. This module's job is to make that lane a matter of adding a class,
not of reworking the profile path.

THE CONNECTOR DOES NOT WRITE THE PROSE
--------------------------------------
Worth stating plainly, because the seam is easy to misread as a generator: the
document is authored UPSTREAM -- by the PoC, by an analyst, by the owner -- and
this module INGESTS it. ``parse_profile_document`` is a pure mapping from the
convention onto ``VoiceProfile``'s fields. It composes no sentences, summarizes
nothing, and calls no model. If the document says something wrong about the
owner, the fix is in the document.

BLOCKQUOTES ARE DROPPED BEFORE ANYTHING IS MAPPED
-------------------------------------------------
A ``profile.md`` quotes its evidence -- that is what makes it auditable, and it
is the right thing for the document to do. It is exactly the wrong thing for a
``VoiceProfile`` to hold: ``profile``'s law is patterns, never content, and a
quoted line is content by definition. So every markdown blockquote line is
removed BEFORE section mapping, not filtered afterwards. Dropping first also
keeps the mapped fields under ``MAX_FIELD_LENGTH`` without truncation, since the
quotes are the bulk of a real document.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from loguru import logger

from inbox_zero_connector.config import VoiceRouting
from inbox_zero_connector.services.voice import profile

PROFILE_DOCUMENT_NAME: Final = "profile.md"
"""The one filename an external corpus directory is read for."""

SECTION_FIELDS: Final[dict[str, str]] = {
    "register": "formality",
    "sentence habits": "sentence_length",
    "how they explain": "structure",
}
"""``## `` heading (lowercased) -> ``VoiceProfile`` field it populates.

A declared constant rather than an ``if`` chain so the mapping is one readable
table: a reader asking "what does ``## Register`` become?" reads a line, and a
lane adding a section edits data. Sections absent from this table -- ``Lexicon``,
``Stance``, ``Ad residue`` -- are deliberately unmapped: they are corpus
provenance, not writing patterns a draft is steered by.
"""

ANTI_PATTERNS_HEADING: Final = "anti-patterns"
"""Its numbered list becomes ``never_does``, so it maps by list item, not text."""

PREAMBLE_CORPUS_PREFIXES: Final = ("corpus:", "written corpus:")
"""Preamble paragraphs whose counts and date bounds become ``sample_summary``."""

PREAMBLE_CONFIDENCE_PREFIX: Final = "confidence:"
"""The preamble paragraph naming what the corpus did NOT cover -> ``unknowns``."""

_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_SIGN_OFF_LINE = re.compile(r"^-\s+OBSERVED\s+—\s+Sign-off:\s*(?P<text>.+)$")
_GREETING_LINE = re.compile(r"^-\s+(?:INFERRED|OBSERVED)\s+—\s+Greeting")
_LIST_ITEM = re.compile(r"^\d+\.\s+(?P<text>.+)$")
_BOLD = re.compile(r"\*\*")
_BLANK_RUN = re.compile(r"\n{3,}")


class CorpusDocumentError(ValueError):
    """A corpus yielded nothing usable, or nothing in the expected convention."""


@runtime_checkable
class CorpusSource(Protocol):
    """Anything that can hand over one profile document.

    One method on purpose. A source's whole job is to produce the document; how
    it got there -- a directory read, a sent-mail sample rendered to prose -- is
    the implementation's business and never the caller's. Widening this Protocol
    is how the caller's deterministic choice would turn into a negotiation.
    """

    def profile_document(self) -> str:
        """Return the profile document as text.

        Raises:
            CorpusDocumentError: If the source has no usable document.
        """
        ...


class ExternalCorpusDir:
    """A directory of externally-authored material holding a ``profile.md``.

    The day-one source: an owner with no sent-mail history still has published
    writing, transcripts, or an onboarding interview, and the PoC turns those
    into the document this reads. Holds a path and nothing else -- there is no
    cached text, so re-reading picks up an edited document rather than serving a
    stale one from the last call.
    """

    def __init__(self, directory: Path) -> None:
        """Bind the corpus directory. Nothing is read until ``profile_document``.

        Args:
            directory: Directory expected to contain ``profile.md``.
        """
        self.directory = directory

    def profile_document(self) -> str:
        """Read ``<directory>/profile.md``.

        Returns:
            The document text, as written.

        Raises:
            CorpusDocumentError: If the directory or file is missing, unreadable,
                or the file is empty or whitespace only. The path is named in
                every case, because the recovery is always "look at that file"
                and an error that withholds which one costs a search.
        """
        target = self.directory / PROFILE_DOCUMENT_NAME
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as error:
            raise CorpusDocumentError(
                f"no readable profile document at {target}: {error}. An external corpus "
                f"directory holds a {PROFILE_DOCUMENT_NAME} describing how the owner writes."
            ) from error
        if not text.strip():
            raise CorpusDocumentError(
                f"the profile document at {target} is empty. A voice profile is built from a "
                "written description of the owner's patterns, not from an empty file."
            )
        logger.debug(f"VOICE: corpus document read path={target} char_count={len(text)}")
        return text


def parse_profile_document(text: str) -> dict[str, object]:
    """Map a profile document onto ``VoiceProfile`` fields. Pure; no I/O.

    Blockquote lines are dropped first (see the module docstring), then the
    remainder is split on ``## `` headings and mapped through ``SECTION_FIELDS``,
    the anti-patterns list, and the two marked written-register lines.

    ``owner_corrections`` is never produced. That field records what the OWNER
    said about themselves and outranks any sample; a parser inferring it from a
    corpus would let the corpus overrule the person it describes.

    Args:
        text: The profile document.

    Returns:
        Field name -> value, omitting any field the document did not cover. A
        missing section is a fact about the corpus, not an error, and omitting
        the key lets ``VoiceProfile``'s own default stand.

    Raises:
        CorpusDocumentError: If no mapped section or marked line was found at
            all, or if a mapped value exceeds ``profile.MAX_FIELD_LENGTH``.
            Over-length is refused rather than truncated: a silently cut
            description of how someone writes is wrong in a way nobody sees.
    """
    lines = [line for line in text.splitlines() if not line.strip().startswith(">")]
    fields: dict[str, object] = {}

    preamble, sections = _split_sections(lines)
    _map_preamble(preamble, fields)
    _map_sections(sections, fields)
    _map_marked_lines(lines, fields)

    if not fields:
        raise CorpusDocumentError(
            "not a profile document in the expected convention: no '## ' section this parser "
            f"maps ({sorted(SECTION_FIELDS)}, {ANTI_PATTERNS_HEADING!r}), no 'Corpus:' or "
            "'Confidence:' preamble, and no OBSERVED/INFERRED sign-off or greeting line."
        )
    _refuse_over_cap(fields)
    return fields


def _split_sections(lines: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Split lines into the pre-heading preamble and ``## ``-headed sections.

    Headings are matched case-insensitively and keyed lowercased, so a document
    written ``## Sentence Habits`` maps the same as ``## Sentence habits``. A
    repeated heading keeps the FIRST occurrence: the convention has one of each,
    and quietly concatenating a duplicate would build a field from two sections
    a reader believes are separate.
    """
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        match = _HEADING.match(line)
        if match:
            title = match.group("title").strip().lower()
            current = sections.setdefault(title, [])
            continue
        (preamble if current is None else current).append(line)
    return preamble, sections


def _map_preamble(preamble: list[str], fields: dict[str, object]) -> None:
    """Pull ``sample_summary`` and ``unknowns`` out of the pre-heading block."""
    corpus = _paragraphs_starting_with(preamble, PREAMBLE_CORPUS_PREFIXES)
    if corpus:
        fields["sample_summary"] = _tidy("\n\n".join(corpus))
    confidence = _paragraphs_starting_with(preamble, (PREAMBLE_CONFIDENCE_PREFIX,))
    if confidence:
        fields["unknowns"] = _tidy("\n\n".join(confidence))


def _paragraphs_starting_with(lines: list[str], prefixes: tuple[str, ...]) -> list[str]:
    """Blank-line-separated paragraphs whose first line starts with a prefix.

    Taking the whole paragraph is what keeps a ``Corpus:`` line together with the
    per-file bullet counts under it, which is where the counts and date bounds
    actually live. Matching is on the paragraph's FIRST line only, so a bullet
    that happens to mention the word does not drag an unrelated block in.
    """
    kept: list[str] = []
    for paragraph in _paragraphs(lines):
        head = paragraph[0].strip().lower()
        if any(head.startswith(prefix) for prefix in prefixes):
            kept.append("\n".join(paragraph))
    return kept


def _paragraphs(lines: list[str]) -> list[list[str]]:
    """Group lines into blank-line-separated paragraphs, dropping empty runs."""
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _map_sections(sections: dict[str, list[str]], fields: dict[str, object]) -> None:
    """Map ``## ``-headed sections onto their fields, prose and list alike."""
    for title, body in sections.items():
        field = SECTION_FIELDS.get(title)
        if field:
            tidied = _tidy("\n".join(body))
            if tidied:
                fields[field] = tidied
        elif title == ANTI_PATTERNS_HEADING:
            items = _numbered_items(body)
            if items:
                fields["never_does"] = items


def _numbered_items(body: list[str]) -> tuple[str, ...]:
    """The section's numbered list, one entry per item, bold markers stripped.

    Continuation lines are folded into the item they belong to, because an
    anti-pattern in the convention runs to two or three wrapped lines and half of
    one is not a rule. Bold markers go because ``**Never ...**`` is presentation
    for a human reading markdown; the stored pattern is the sentence.
    """
    items: list[list[str]] = []
    for line in body:
        match = _LIST_ITEM.match(line.strip())
        if match:
            items.append([match.group("text").strip()])
        elif items and line.strip():
            items[-1].append(line.strip())
    return tuple(_BOLD.sub("", " ".join(parts)).strip() for parts in items)


def _map_marked_lines(lines: list[str], fields: dict[str, object]) -> None:
    """Pull ``sign_off`` and ``greeting`` from the first line marked for each.

    The FIRST match wins for each, matching how the convention reads: the
    written-register section states the invariant once and elaborates after.

    ``greeting`` KEEPS whichever marker the document carried. That is the ``#30``
    convention and it is load-bearing rather than cosmetic: a private one-to-one
    greeting is the one thing a BROADCAST corpus structurally cannot observe, so
    a profile that presents that guess as observed invites the owner to approve a
    fact nobody has. A SENT-MAIL corpus is the case where the marker legitimately
    flips: it reads the owner's own one-to-one mail and counts the salutations
    directly, so ``OBSERVED`` is a legal greeting marker here and the stored
    value must not launder it back to inferred -- or forward. ``sign_off`` drops
    its marker because the text after ``Sign-off:`` IS the observation.
    """
    for line in lines:
        stripped = line.strip()
        if "sign_off" not in fields:
            match = _SIGN_OFF_LINE.match(stripped)
            if match:
                fields["sign_off"] = match.group("text").strip()
                continue
        if "greeting" not in fields and _GREETING_LINE.match(stripped):
            fields["greeting"] = stripped.lstrip("- ").strip()


def _tidy(text: str) -> str:
    """Strip trailing spaces and collapse blank-line runs. Never rewraps.

    Deliberately minimal. The document's own line breaks are how its author
    chose to present the description, and a parser that rewrapped prose would be
    editing the owner's profile on their behalf under the name of formatting.
    """
    body = "\n".join(line.rstrip() for line in text.splitlines())
    return _BLANK_RUN.sub("\n\n", body).strip()


def _refuse_over_cap(fields: dict[str, object]) -> None:
    """Raise naming any field over ``profile.MAX_FIELD_LENGTH``, with its length.

    Naming the length as well as the field is what makes the error actionable in
    one read: the fix is always to shorten that section in the document, and the
    author needs to know by how much.
    """
    for name, value in fields.items():
        for text in value if isinstance(value, tuple) else (value,):
            if len(str(text)) > profile.MAX_FIELD_LENGTH:
                raise CorpusDocumentError(
                    f"profile document field {name!r} is {len(str(text))} characters, over the "
                    f"{profile.MAX_FIELD_LENGTH}-character limit. A voice profile is a page of "
                    "patterns, not a corpus. Shorten that section in the document -- this is "
                    "refused rather than truncated, because a silently cut description of how "
                    "someone writes is wrong in a way nobody sees."
                )


def build_profile(
    source: CorpusSource,
    *,
    voice: str,
    routing: VoiceRouting,
    now: datetime,
) -> profile.VoiceProfile:
    """Build one voice profile from a corpus source. Writes nothing to disk.

    The whole path in three steps: check the voice is one the installer
    configured, parse the source's document, and validate the result through
    ``profile.validate`` so a disclosing field is refused with the offending
    field named rather than wrapped in a pydantic error.

    NOTHING HERE PERSISTS. Persisting goes through ``profile.store`` at the
    caller's hand, which stamps the row UNAPPROVED on every write -- so a profile
    built from a corpus lands exactly where one built from sent mail does, and
    the owner still reads it before anything drafts with it. The approval gate is
    untouched by this module; adding a save here is what would quietly move it.

    Args:
        source: Where the profile document comes from.
        voice: Voice name; normalized and checked against ``routing.voices``.
        routing: The installed routing table, which is the list of legal names.
        now: Build timestamp, a parameter so a test asserts the time it chose.

    Returns:
        An unapproved ``VoiceProfile`` for ``voice``.

    Raises:
        ValueError: If ``voice`` is not an installed voice.
        CorpusDocumentError: If the source or its document is unusable.
        profile.ProfileDisclosureError: If a parsed field carries an address.
    """
    name = profile.validate_voice_name(routing, voice)
    fields = parse_profile_document(source.profile_document())
    built = profile.validate(fields | {"voice": name, "built_at": now.isoformat()})
    logger.info(f"VOICE: profile built from corpus voice={name} field_count={len(fields)}")
    return built
