"""Tests for the ``voice`` CLI subcommand: show, approve, build.

This is the surface the APPROVAL GATE is reached through. ``profile`` refuses
to hand an unapproved row to drafting and names a command in the refusal; until
that command exists, the refusal tells the owner to type something untypable.
So the tests that matter here are end-to-end through ``cli.main`` -- parser row,
dispatch table, store write, exit code -- rather than calls to ``profile.approve``,
which is already covered by ``tests/services/voice/test_voiceprofile.py`` and
would prove nothing about the shipped verb.

Nothing here auto-approves. ``build`` stores UNAPPROVED and the test asserts it,
because a build path that quietly approved its own output would make the gate
decorative while every other test still passed.

The autouse ``isolated_dirs`` fixture in ``conftest.py`` redirects
``INBOX_ZERO_CONFIG_DIR`` at a ``tmp_path``, so ``voice_profiles_path()``
resolves inside the test's own sandbox. No mail, no model, no network.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.apps.cli import voice as voice_cli
from inbox_zero_connector.config import VoiceRouting
from inbox_zero_connector.providers.base import ProviderError, SentMessage, SentSelection
from inbox_zero_connector.services.voice.profile import (
    APPROVE_COMMAND,
    VoiceProfile,
    load,
    load_for_drafting,
    save,
)
from inbox_zero_connector.services.voice.sentmail import MIN_SUBSTANTIVE_MESSAGES

VOICE = "external"

PROFILE_DOCUMENT = """\
Corpus: 12 published posts, 3 recorded talks, 2026-01 to 2026-06.

Confidence: no private one-to-one correspondence in the corpus, so the
greeting is inferred rather than observed.

## Register

Contractions throughout. Direct rather than hedged.

- OBSERVED — Sign-off: one word on its own line, then the first name.
- INFERRED — Greeting: first name and a comma, no greeting word.

## Sentence habits

Short. Most under fifteen words.

## Anti-patterns

1. Never uses exclamation marks.
2. Never opens with an apology.
"""


@pytest.fixture
def routing() -> VoiceRouting:
    """The installed routing table, which is the list of legal voice names."""
    return VoiceRouting(
        owner_domain="rtech-consulting.com",
        voices=("internal", "external", "sales"),
        sales_labels=("prospect",),
    )


@pytest.fixture
def _installed_routing(routing: VoiceRouting, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``load_config().require_routing()`` answer with ``routing``.

    ``voice build`` reads routing to check the voice is one an installer
    configured. Patching the loader rather than writing a config file keeps the
    test about the CLI verb instead of about config layering, which has its own
    tests.
    """

    class _Config:
        def require_routing(self) -> VoiceRouting:
            return routing

    monkeypatch.setattr(voice_cli, "load_config", lambda: _Config())


def _store_unapproved(voice: str = VOICE) -> VoiceProfile:
    """Put one unapproved row on disk and return it."""
    profile = VoiceProfile(
        voice=voice,
        greeting="First name and a comma. No greeting word.",
        sign_off="One word on its own line, then the first name.",
        sentence_length="Short. Most under fifteen words.",
        formality="Contractions throughout. Direct rather than hedged.",
        structure="Two to four short paragraphs.",
        never_does=("No exclamation marks.", "No emoji."),
        sample_summary="58 read, 31 usable, six-month bound.",
        built_at="2026-08-19T12:00:00+00:00",
    )
    save({voice: profile})
    return profile


def test_approve_flips_the_row_on_disk(capsys: pytest.CaptureFixture[str]) -> None:
    """The approval lands in the FILE, through the shipped command.

    Asserting on a reload rather than on stdout is the point: a command that
    printed "approved" without saving would pass a stdout-only test and leave
    drafting still refused.
    """
    _store_unapproved()

    assert cli.main(["voice", "approve", VOICE]) == 0

    reloaded = load()[VOICE]
    assert reloaded.approved is True
    assert reloaded.approved_at
    assert VOICE in capsys.readouterr().out


def test_approve_opens_the_drafting_gate(routing: VoiceRouting) -> None:
    """After the CLI approve, ``load_for_drafting`` stops refusing."""
    _store_unapproved()

    assert cli.main(["voice", "approve", VOICE]) == 0

    assert load_for_drafting(routing, VOICE).approved is True


def test_approve_unknown_voice_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    """The store's own not-found message reaches stderr."""
    assert cli.main(["voice", "approve", "internal"]) == 1

    assert "internal" in capsys.readouterr().err


def test_show_prints_the_fields_and_the_unapproved_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show is what the owner READS before approving, so every field is on it."""
    _store_unapproved()

    assert cli.main(["voice", "show", VOICE]) == 0

    out = capsys.readouterr().out
    assert "First name and a comma" in out
    assert "One word on its own line" in out
    assert "No exclamation marks." in out
    assert "not approved" in out.lower()


def test_show_unknown_voice_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["voice", "show", "internal"]) == 1
    assert "internal" in capsys.readouterr().err


@pytest.mark.usefixtures("_installed_routing")
def test_build_stores_unapproved(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A built profile lands UNAPPROVED. The build path does not consent for the owner."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "profile.md").write_text(PROFILE_DOCUMENT, encoding="utf-8")

    assert cli.main(["voice", "build", VOICE, "--from-corpus", str(corpus)]) == 0

    stored = load()[VOICE]
    assert stored.approved is False
    assert stored.formality
    assert "voice approve" in capsys.readouterr().out


@pytest.mark.usefixtures("_installed_routing")
def test_build_names_a_missing_corpus_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty corpus directory is the corpus module's error, on stderr."""
    empty = tmp_path / "empty"
    empty.mkdir()

    assert cli.main(["voice", "build", VOICE, "--from-corpus", str(empty)]) == 1

    assert "profile.md" in capsys.readouterr().err


def _sent_message(index: int, body: str) -> SentMessage:
    """One sent row. Bodies are long enough to be substantive unless told not to."""
    return SentMessage(
        id=f"m{index}",
        thread_id=f"t{index}",
        recipients=["client@example.com"],
        subject=f"Matter {index}",
        sent=f"2026-06-{index % 28 + 1:02d}T09:00:00+00:00",
        body=body,
    )


SUBSTANTIVE_BODY = (
    "Hi Dana,\n\n"
    "Here is where the filing stands and what I need from you before Friday. "
    "The exhibits are indexed and the declaration is drafted. I will send the "
    "redline once your numbers land.\n\n"
    "Thanks\nRico\n"
)


@pytest.fixture
def _installed_provider(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[SentMessage]], None]:
    """Install a fake provider in the VOICE CLI namespace and return its loader.

    The fake's ``read_sent_sample`` enforces the SAME empty-selection refusal the
    shipped providers do, because that refusal is what test 4 asserts reaches the
    operator -- a fake that answered an empty selection would prove the CLI wired
    a source, not that the boundary held.
    """

    def install(messages: list[SentMessage]) -> None:
        class _Provider:
            def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
                if selection.is_empty():
                    raise ProviderError(
                        "read_sent_sample requires an explicit selection. Refusing to read "
                        "the whole sent folder. ACTION REQUIRED: pass labels, a date range, "
                        "or message_ids."
                    )
                return messages[: selection.limit]

        class _Connector:
            def drafts_on(self, provider: str) -> bool:
                return False

        class _Settings:
            provider = "google"

            def connector_config(self) -> _Connector:
                return _Connector()

        monkeypatch.setattr(voice_cli, "load_settings", lambda: _Settings())
        monkeypatch.setattr(voice_cli, "get_provider", lambda name, **kwargs: _Provider())

    return install


@pytest.mark.usefixtures("_installed_routing")
def test_build_from_sent_flags_stores_unapproved(
    _installed_provider: Callable[[list[SentMessage]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sent-mail source lands in exactly the same UNAPPROVED state as a directory."""
    _installed_provider([_sent_message(index, SUBSTANTIVE_BODY) for index in range(14)])

    assert cli.main(["voice", "build", VOICE, "--sent-label", "sent", "--sent-limit", "50"]) == 0

    stored = load()[VOICE]
    assert stored.approved is False
    assert stored.formality
    assert "voice approve" in capsys.readouterr().out


@pytest.mark.usefixtures("_installed_routing")
def test_build_refuses_both_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two sources is refused, not ranked. Nothing is stored."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "profile.md").write_text(PROFILE_DOCUMENT, encoding="utf-8")

    exit_code = cli.main([
        "voice",
        "build",
        VOICE,
        "--from-corpus",
        str(corpus),
        "--sent-label",
        "sent",
    ])

    assert exit_code == 1
    assert "exactly one source" in capsys.readouterr().err
    assert VOICE not in load()


@pytest.mark.usefixtures("_installed_routing")
def test_build_with_no_source_refuses_and_names_both(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flags NEVER means an implicit mailbox read; the error names both options."""
    assert cli.main(["voice", "build", VOICE]) == 1

    err = capsys.readouterr().err
    assert "--from-corpus" in err
    assert "--sent-label" in err
    assert VOICE not in load()


@pytest.mark.usefixtures("_installed_routing")
def test_build_with_only_a_limit_surfaces_the_empty_selection_refusal(
    _installed_provider: Callable[[list[SentMessage]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A limit narrows nothing. The PROVIDER's refusal reaches stderr verbatim.

    Not silently rerouted to the external path and not pre-empted by a CLI
    message of its own: the boundary that refuses an unbounded sent-folder read
    is the provider's, and the operator should read that sentence.
    """
    _installed_provider([_sent_message(index, SUBSTANTIVE_BODY) for index in range(14)])

    assert cli.main(["voice", "build", VOICE, "--sent-limit", "10"]) == 1

    err = capsys.readouterr().err
    assert "requires an explicit selection" in err
    assert "pass labels, a date range, or message_ids" in err
    assert VOICE not in load()


@pytest.mark.usefixtures("_installed_routing")
def test_build_surfaces_the_thin_sample_floor(
    _installed_provider: Callable[[list[SentMessage]], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under the floor, ``sentmail``'s own refusal is the line the operator gets."""
    _installed_provider([_sent_message(index, SUBSTANTIVE_BODY) for index in range(3)])

    assert cli.main(["voice", "build", VOICE, "--sent-after", "2026-01-01"]) == 1

    err = capsys.readouterr().err
    assert f"under the {MIN_SUBSTANTIVE_MESSAGES}-message" in err
    assert "external corpus directory" in err
    assert VOICE not in load()


def test_approve_command_names_a_real_entrypoint() -> None:
    """``pyproject`` ships exactly one console script, and this must name it.

    The old value said ``inbox-zero voice approve``, a binary that does not
    exist, so the refusal every unapproved profile raises told the owner to type
    something their shell would not find.
    """
    assert APPROVE_COMMAND == "inbox-zero-connector voice approve <voice>"
