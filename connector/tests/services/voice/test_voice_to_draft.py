"""The WIRE between the profile store and the composed draft, end to end.

Sprint 4 built two halves and tested each in isolation. ``test_voiceprofile.py``
proves a profile round-trips, refuses disclosure, and gates on approval.
``test_drafting.py`` proves the prompt fences untrusted text and the constraint
gate holds. Neither proves that the profile's CONTENT reaches the draft path and
shapes what comes out -- that a DIFFERENT stored profile produces a DIFFERENT
prompt. That seam is what this file covers.

The assertion surface is the RENDERED PROMPT STRING every time, never a call
count and never a module internal. A test that asserts ``build_body_prompt`` was
CALLED proves the wire is connected; a test that asserts the owner's invented
sign-off is IN the string proves the wire carries something.

EVERY FIXTURE HERE IS SYNTHETIC AND OBVIOUSLY SO. The owner is a fictional
person, the habits are invented, and the sign-off strings are nonsense words
chosen so that a grep for them can only match this file. That is deliberate on
two counts: nothing reads the real profile store at
``~/Library/Application Support/inbox-zero-connector/voice-profiles.json``, and
nobody reading a failure later mistakes a fixture for a measured fact about how
Rico writes. Pinning a test to real measured habits would also make it fail as
soon as the owner sent more mail, which is a test that decays by design.

Every test drives ``path=`` explicitly into a ``tmp_path`` store. The real
config directory is never resolved, never read, and never written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_zero_connector.config import VoiceRouting, resolve_voice
from inbox_zero_connector.services.voice import drafting as drafting
from inbox_zero_connector.services.voice import profile as voiceprofile
from inbox_zero_connector.services.voice.profile import APPROVE_COMMAND, VoiceProfile

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

# Invented sign-off tokens. Nonsense on purpose: a real owner's sign-off would
# be a common word, and a common word in an assertion cannot distinguish "the
# profile reached the prompt" from "the prompt happened to contain that word".
BUSINESS_SIGNOFF = "closes with the token Quillfarrow on its own line"
PERSONAL_SIGNOFF = "closes with the token Bramblewick and nothing after it"


class _Backend:
    """A composer with no model behind it. Records prompts, returns fixed text."""

    def __init__(self, body: str = "Noted, will follow up.") -> None:
        self.body = body
        self.prompts: list[str] = []
        self.voices: list[VoiceProfile] = []

    def compose(self, prompt: str, voice: VoiceProfile) -> str:
        self.prompts.append(prompt)
        self.voices.append(voice)
        return self.body


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    """The synthetic store. Never the installed one."""
    return tmp_path / "voice-profiles.json"


@pytest.fixture
def routing() -> VoiceRouting:
    """A fictional installer that configured a personal voice beside the rest.

    ``example.test`` is a reserved name that resolves nowhere, so a fixture can
    never be mistaken for a real correspondent domain.
    """
    return VoiceRouting(
        owner_domain="owner.example.test",
        voices=("internal", "external", "sales", "personal"),
        sales_labels=("prospect",),
    )


def _profile(voice: str, **overrides: object) -> VoiceProfile:
    """A synthetic profile for ``voice``. Invented habits, fictional owner."""
    base: dict[str, object] = {
        "voice": voice,
        "greeting": "opens with the recipient's first name and a comma",
        "sign_off": BUSINESS_SIGNOFF,
        "sentence_length": "short, most under fifteen words",
        "formality": "contractions throughout, direct rather than hedged",
        "structure": "two to four short paragraphs, no bullets",
        "never_does": ("uses exclamation marks", "uses emoji"),
        "sample_summary": "58 read, 31 usable, six-month bound",
    }
    return voiceprofile.validate(base | overrides)


def _approved(store: Path, *profiles: VoiceProfile) -> None:
    """Store and approve each profile into ``store``. Approval is the only path.

    Goes through ``store`` then ``approve`` rather than constructing a row with
    ``approved=True``, because ``approve`` is the only function the module lets
    raise that flag and a fixture that forged it would test a state the product
    cannot reach.
    """
    table: dict[str, VoiceProfile] = {}
    for profile in profiles:
        table = voiceprofile.store(table, profile, now=NOW)
        table = voiceprofile.approve(table, profile.voice, now=NOW)
    voiceprofile.save(table, store)


# ---------------------------------------------------------------------------
# 1. An approved profile's habits reach the rendered prompt.
# ---------------------------------------------------------------------------


def test_an_approved_profiles_habits_appear_in_the_prompt_that_is_built(
    store_path: Path, routing: VoiceRouting
) -> None:
    """The wire, asserted on the rendered string the backend actually receives.

    ``generate_draft_body`` is the entry point rather than ``build_body_prompt``
    on purpose: calling the builder directly with a hand-made profile would
    prove the builder renders habits and prove nothing about whether the STORED
    profile is the one that gets there.
    """
    _approved(store_path, _profile("external"))
    backend = _Backend()

    drafting.generate_draft_body(
        backend, "can you take a look at this", routing, "external", path=store_path
    )

    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert BUSINESS_SIGNOFF in prompt
    assert "opens with the recipient's first name and a comma" in prompt
    assert "short, most under fifteen words" in prompt
    assert "contractions throughout, direct rather than hedged" in prompt
    assert "two to four short paragraphs, no bullets" in prompt
    assert "never uses emoji" in prompt


# ---------------------------------------------------------------------------
# 2. An unapproved profile does not reach the draft path.
# ---------------------------------------------------------------------------


def test_an_unapproved_profile_never_reaches_the_composer_and_names_the_verb(
    store_path: Path, routing: VoiceRouting
) -> None:
    """Stored but unread: the refusal lands BEFORE anything is composed.

    ``backend.prompts == []`` is the load-bearing assertion. An exception raised
    after the backend had already been handed the owner's writing patterns would
    still be an exception, and would still have leaked the profile into whatever
    the composer is; the empty list is what proves the gate is upstream of it.

    The error names ``APPROVE_COMMAND`` with the voice substituted, because an
    owner reading "not approved" already wants to fix it and should not have to
    go hunting for the verb.
    """
    table = voiceprofile.store({}, _profile("external"), now=NOW)
    voiceprofile.save(table, store_path)
    backend = _Backend()

    with pytest.raises(voiceprofile.ProfileNotApprovedError) as raised:
        drafting.generate_draft_body(
            backend, "can you take a look at this", routing, "external", path=store_path
        )

    assert backend.prompts == []
    assert APPROVE_COMMAND.replace("<voice>", "external") in str(raised.value)


def test_rewriting_an_approved_profile_re_locks_the_draft_path(
    store_path: Path, routing: VoiceRouting
) -> None:
    """``store`` clears approval, so an EDITED profile is unread by definition.

    The positive control comes first: the same voice drafts fine while approved.
    Without it, the refusal below would pass on a store that was simply broken.
    """
    _approved(store_path, _profile("external"))
    backend = _Backend()
    drafting.generate_draft_body(backend, "first pass", routing, "external", path=store_path)
    assert len(backend.prompts) == 1

    rewritten = voiceprofile.store(
        voiceprofile.load(store_path),
        _profile("external", sign_off="closes with the token Fennelgate"),
        now=NOW,
    )
    voiceprofile.save(rewritten, store_path)

    with pytest.raises(voiceprofile.ProfileNotApprovedError):
        drafting.generate_draft_body(backend, "second pass", routing, "external", path=store_path)
    assert len(backend.prompts) == 1, "the rewritten profile never reached the composer"


# ---------------------------------------------------------------------------
# 3. resolve_voice is the selector, and two routings select two profiles.
# ---------------------------------------------------------------------------


def test_two_different_routing_inputs_select_two_distinguishable_prompts(
    store_path: Path, routing: VoiceRouting
) -> None:
    """``resolve_voice`` picks the row; the row picks the prompt. Both observed.

    Routing is driven by RECIPIENT DOMAINS, which is a pure function of headers
    with no message body in it at all -- so the two prompts differ because the
    thread went to different people, never because a model preferred a register.
    """
    _approved(
        store_path,
        _profile("internal", sign_off="closes with the token Dunnycove"),
        _profile("external", sign_off=BUSINESS_SIGNOFF),
    )

    internal = resolve_voice(routing, ["colleague@owner.example.test"])
    external = resolve_voice(routing, ["stranger@other.example.test"])
    assert (internal, external) == ("internal", "external")

    prompts = []
    for voice in (internal, external):
        backend = _Backend()
        drafting.generate_draft_body(backend, "same thread text", routing, voice, path=store_path)
        prompts.append(backend.prompts[0])

    assert "closes with the token Dunnycove" in prompts[0]
    assert BUSINESS_SIGNOFF not in prompts[0]
    assert BUSINESS_SIGNOFF in prompts[1]
    assert "closes with the token Dunnycove" not in prompts[1]


def test_a_sales_label_routes_to_the_sales_row_on_the_same_recipients(
    store_path: Path, routing: VoiceRouting
) -> None:
    """The label, not the body, moves the selection -- and moves the prompt with it."""
    _approved(
        store_path,
        _profile("external", sign_off=BUSINESS_SIGNOFF),
        _profile("sales", sign_off="closes with the token Marrowgate"),
    )
    recipients = ["buyer@other.example.test"]

    plain = resolve_voice(routing, recipients)
    labelled = resolve_voice(routing, recipients, ["prospect"])
    assert (plain, labelled) == ("external", "sales")

    backend = _Backend()
    drafting.generate_draft_body(backend, "pricing question", routing, labelled, path=store_path)
    assert "closes with the token Marrowgate" in backend.prompts[0]
    assert BUSINESS_SIGNOFF not in backend.prompts[0]


# ---------------------------------------------------------------------------
# 4. The profile actually matters: one field differs, the prompts differ there.
# ---------------------------------------------------------------------------


def test_two_profiles_differing_only_in_sign_off_yield_prompts_differing_there(
    store_path: Path, routing: VoiceRouting
) -> None:
    """The isolation test. Everything else is held identical on purpose.

    Two rows built from the same base with ONE field changed. If the prompts
    differ anywhere other than that field, the difference did not come from the
    profile, and the assertion on the symmetric difference is what catches it.

    R1 (2026-08-19) is the reason the personal row exists at all: making
    ``Personal-Action`` a drafting folder without a personal register would emit
    birthday-party replies in the owner's client-facing voice.
    """
    _approved(
        store_path,
        _profile("external", sign_off=BUSINESS_SIGNOFF),
        _profile("personal", sign_off=PERSONAL_SIGNOFF),
    )

    rendered: dict[str, str] = {}
    for voice in ("external", "personal"):
        backend = _Backend()
        drafting.generate_draft_body(backend, "identical thread", routing, voice, path=store_path)
        rendered[voice] = backend.prompts[0]

    assert rendered["external"] != rendered["personal"]
    assert BUSINESS_SIGNOFF in rendered["external"]
    assert PERSONAL_SIGNOFF in rendered["personal"]
    assert PERSONAL_SIGNOFF not in rendered["external"]
    assert BUSINESS_SIGNOFF not in rendered["personal"]

    # The ONLY difference is the sign-off line. Anything else diverging means
    # something other than the changed field leaked into the prompt.
    business_only = set(rendered["external"].splitlines()) - set(rendered["personal"].splitlines())
    personal_only = set(rendered["personal"].splitlines()) - set(rendered["external"].splitlines())
    assert business_only == {f"- sign-off: {BUSINESS_SIGNOFF}"}
    assert personal_only == {f"- sign-off: {PERSONAL_SIGNOFF}"}


# ---------------------------------------------------------------------------
# 5. No profile stored for the routed voice.
# ---------------------------------------------------------------------------


def test_a_routed_voice_with_no_stored_profile_refuses_rather_than_drafting_voiceless(
    store_path: Path, routing: VoiceRouting
) -> None:
    """The absent-profile behaviour, asserted rather than assumed.

    NOT A FINDING. The observed behaviour is a ``ProfileNotFoundError`` raised
    before the backend is reached, which is the right answer: silently composing
    with no voice would produce a draft in the model's house style and present
    it to the owner as their own writing, and that draft is indistinguishable
    from a good one until they read it closely.

    ``personal`` is routable here -- it is in ``routing.voices`` -- so this is
    genuinely "the installer configured a register and never built its profile",
    not a name-validation failure wearing the same exception.
    """
    _approved(store_path, _profile("external"))
    backend = _Backend()

    with pytest.raises(voiceprofile.ProfileNotFoundError) as raised:
        drafting.generate_draft_body(
            backend, "happy birthday thread", routing, "personal", path=store_path
        )

    assert backend.prompts == []
    assert "personal" in str(raised.value)


def test_an_entirely_empty_store_refuses_every_routed_voice(
    store_path: Path, routing: VoiceRouting
) -> None:
    """First run: nothing stored at all, and the file does not exist yet."""
    assert not store_path.exists()
    backend = _Backend()

    with pytest.raises(voiceprofile.ProfileNotFoundError):
        drafting.generate_draft_body(backend, "anything", routing, "external", path=store_path)

    assert backend.prompts == []


def test_a_voice_outside_the_installed_list_refuses_before_the_store_is_read(
    store_path: Path, routing: VoiceRouting
) -> None:
    """A different failure with a different error, so the two cannot be confused.

    An unconfigured NAME is a ``ValueError`` from ``validate_voice_name``; a
    configured name with no row is ``ProfileNotFoundError`` above. Asserting
    both keeps the previous test honest about which precondition it proved.
    """
    _approved(store_path, _profile("external"))
    backend = _Backend()

    with pytest.raises(ValueError) as raised:
        drafting.generate_draft_body(backend, "anything", routing, "unconfigured", path=store_path)

    assert not isinstance(raised.value, voiceprofile.ProfileNotFoundError)
    assert backend.prompts == []


# ---------------------------------------------------------------------------
# 6. Owner corrections outrank a conflicting measured habit.
# ---------------------------------------------------------------------------


def test_owner_corrections_reach_the_prompt_after_the_habit_they_contradict(
    store_path: Path, routing: VoiceRouting
) -> None:
    """The correction is rendered, and it is rendered LAST of the two.

    ``owner_corrections`` is documented as outranking the sample, and the
    prompt's only mechanism for expressing rank is ORDER: the corrections block
    is emitted after the habits block, so a later line contradicting an earlier
    one is the last thing the composer reads on that subject.

    Position is asserted with ``.index``, not merely membership. Membership
    would pass if the builder emitted the correction first, which is the
    ordering that loses the conflict.
    """
    _approved(
        store_path,
        _profile(
            "external",
            sign_off="closes with the token Quillfarrow, measured from the sample",
            owner_corrections=(
                "the owner says they no longer sign off with Quillfarrow, use Hollowmere",
            ),
        ),
    )
    backend = _Backend()

    drafting.generate_draft_body(backend, "please advise", routing, "external", path=store_path)

    prompt = backend.prompts[0]
    measured = "closes with the token Quillfarrow, measured from the sample"
    correction = "the owner says they no longer sign off with Quillfarrow, use Hollowmere"
    assert measured in prompt
    assert correction in prompt
    assert prompt.index(correction) > prompt.index(measured)


def test_a_profile_with_no_corrections_renders_no_correction_lines(
    store_path: Path, routing: VoiceRouting
) -> None:
    """The negative control for the test above: absence is not a rendering bug.

    Without this, an assertion that a correction APPEARS could be satisfied by a
    builder that emitted a fixed corrections stanza regardless of content.
    """
    _approved(store_path, _profile("external", owner_corrections=()))
    backend = _Backend()

    drafting.generate_draft_body(backend, "please advise", routing, "external", path=store_path)

    assert "Hollowmere" not in backend.prompts[0]
    assert BUSINESS_SIGNOFF in backend.prompts[0]


# ---------------------------------------------------------------------------
# The store is synthetic. Nothing here can reach the owner's real profile.
# ---------------------------------------------------------------------------


def test_the_generation_path_reads_the_passed_store_and_not_the_installed_one(
    store_path: Path, routing: VoiceRouting, tmp_path: Path
) -> None:
    """``path=`` is honoured, which is what makes every test above isolated.

    The proof is a DECOY: a second store under the same tmp directory holds a
    contradicting profile for the same voice. If ``generate_draft_body`` ignored
    ``path`` and resolved a store of its own, the assertion on which sign-off
    landed would be the thing that caught it.
    """
    decoy = tmp_path / "decoy-profiles.json"
    _approved(store_path, _profile("external", sign_off=BUSINESS_SIGNOFF))
    _approved(decoy, _profile("external", sign_off="closes with the token Sparrowlint"))
    backend = _Backend()

    drafting.generate_draft_body(backend, "please advise", routing, "external", path=store_path)

    assert BUSINESS_SIGNOFF in backend.prompts[0]
    assert "Sparrowlint" not in backend.prompts[0]
