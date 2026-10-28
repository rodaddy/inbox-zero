"""The voice-profile store: round-trip, disclosure refusal, approval, selection.

Four properties are load-bearing and each has its own section below:

* a profile survives save/load unchanged, including the personal row;
* a profile carrying an email address is REFUSED at construction, loudly, by
  the same parse ``config.extract_domain`` uses;
* an unapproved profile cannot be used for drafting, and the refusal names the
  command that approves it;
* ``resolve_voice`` -- not this module and not a model -- decides WHICH profile
  a reply uses, and the internal/external split selects different rows.

Every test drives a temp path explicitly rather than the real config directory.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_zero_connector.config import VoiceRouting, resolve_voice
from inbox_zero_connector.services.voice.profile import (
    APPROVE_COMMAND,
    SCHEMA_VERSION,
    ProfileDisclosureError,
    ProfileNotApprovedError,
    ProfileNotFoundError,
    VoiceProfile,
    approve,
    get,
    load,
    load_for_drafting,
    save,
    scan_for_disclosure,
    store,
    validate,
    validate_voice_name,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "voice-profiles.json"


@pytest.fixture
def routing() -> VoiceRouting:
    """An installer that configured a personal voice alongside the business ones."""
    return VoiceRouting(
        owner_domain="rtech-consulting.com",
        voices=("internal", "external", "sales", "personal"),
        sales_labels=("prospect",),
    )


def _profile(voice: str, **overrides: object) -> VoiceProfile:
    base: dict[str, object] = {
        "voice": voice,
        "greeting": "First name and a comma. No greeting word.",
        "sign_off": "One word on its own line, then the first name.",
        "sentence_length": "Short. Most under fifteen words.",
        "formality": "Contractions throughout. Direct rather than hedged.",
        "structure": "Two to four short paragraphs. No bullets in the sample.",
        "never_does": ("No exclamation marks.", "No emoji."),
        "sample_summary": "58 read, 31 usable, six-month bound.",
    }
    return validate(base | overrides)


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------


def test_profile_round_trips_through_save_and_load(store_path: Path) -> None:
    written = store({}, _profile("external"), now=NOW)
    save(written, store_path)
    assert load(store_path) == written


def test_personal_voice_is_an_ordinary_row(store_path: Path, routing: VoiceRouting) -> None:
    """Rico's R1: Personal-Action drafts, so a personal profile is a row, not a case."""
    profiles = store({}, _profile("external"), now=NOW)
    profiles = store(profiles, _profile("personal"), now=NOW)
    save(profiles, store_path)

    reloaded = load(store_path)
    assert sorted(reloaded) == ["external", "personal"]
    assert validate_voice_name(routing, "personal") == "personal"
    # Same code path, no special case: the personal row approves like any other.
    assert approve(reloaded, "personal", now=LATER)["personal"].approved


def test_load_of_a_missing_file_is_the_first_run(store_path: Path) -> None:
    assert load(store_path) == {}


def test_a_corrupt_row_does_not_cost_the_others(store_path: Path) -> None:
    save(store({}, _profile("external"), now=NOW), store_path)
    payload = store_path.read_text(encoding="utf-8")
    store_path.write_text(
        payload.replace('"profiles": {', '"profiles": {"sales": {"voice": 12345},'),
        encoding="utf-8",
    )
    assert sorted(load(store_path)) == ["external"]


def test_a_row_whose_key_disagrees_with_its_voice_is_dropped(store_path: Path) -> None:
    save(store({}, _profile("external"), now=NOW), store_path)
    store_path.write_text(
        store_path.read_text(encoding="utf-8").replace('"external": {', '"sales": {', 1),
        encoding="utf-8",
    )
    assert load(store_path) == {}


def test_store_leaves_a_profile_unapproved(store_path: Path) -> None:
    profiles = store({}, _profile("external"), now=NOW)
    assert profiles["external"].approved is False
    assert profiles["external"].built_at == NOW.isoformat()


def test_rewriting_an_approved_profile_clears_its_approval() -> None:
    profiles = approve(store({}, _profile("external"), now=NOW), "external", now=LATER)
    assert profiles["external"].approved is True

    rewritten = store(
        profiles, _profile("external", greeting="Now uses a greeting word."), now=LATER
    )
    assert rewritten["external"].approved is False
    assert rewritten["external"].approved_at == ""


def test_get_names_the_stored_voices_when_one_is_missing() -> None:
    with pytest.raises(ProfileNotFoundError, match="external"):
        get(store({}, _profile("external"), now=NOW), "sales")


# ---------------------------------------------------------------------------
# 2. Disclosure refusal -- the mechanical address check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, text",
    [
        ("greeting", "Opens with 'Hi Sam' when writing to sam@acmeparts.com."),
        ("sign_off", "Signs off to priya@example.org with one word."),
        ("formality", "Formal with counsel (counsel@lawfirm.co.uk), casual otherwise."),
        ("sample_summary", "31 usable, mostly to <justin@imperity.com>."),
        ("unknowns", "No angry mail in the sample; closest was ops@vendor.net."),
    ],
)
def test_an_address_in_any_field_is_refused(field: str, text: str) -> None:
    with pytest.raises(ProfileDisclosureError) as caught:
        _profile("external", **{field: text})
    message = str(caught.value)
    assert field in message
    assert "PATTERNS" in message


def test_an_address_in_a_list_entry_is_refused_with_its_index() -> None:
    with pytest.raises(ProfileDisclosureError, match=r"never_does\[1\]"):
        _profile("external", never_does=("No emoji.", "Never CCs legal@acme.com."))


def test_an_address_in_an_owner_correction_is_refused() -> None:
    with pytest.raises(ProfileDisclosureError, match=r"owner_corrections\[0\]"):
        _profile("external", owner_corrections=("Always CC dana@acme.com.",))


def test_the_refusal_quotes_the_token_it_found() -> None:
    with pytest.raises(ProfileDisclosureError, match=re.escape("sam@acmeparts.com")):
        _profile("external", greeting="First name only, e.g. sam@acmeparts.com.")


def test_the_bare_constructor_also_refuses_disclosure() -> None:
    """The rule is a property of the OBJECT, not only of the ``validate`` path.

    Pydantic wraps the typed error, so this asserts on the message rather than
    the class -- which is exactly why ``validate`` exists for callers that need
    to catch it.
    """
    with pytest.raises(ValueError, match="parses as an email address"):
        VoiceProfile(voice="external", greeting="Writes to sam@acmeparts.com.")


def test_a_clean_pattern_description_is_accepted() -> None:
    """The positive control. Without it the refusal tests pass vacuously."""
    profile = _profile("external", greeting="First name and a comma. No greeting word.")
    assert profile.greeting.startswith("First name")


def test_scan_uses_the_extract_domain_parse_and_survives_punctuation() -> None:
    assert scan_for_disclosure("writes to (sam@acmeparts.com).") == "sam@acmeparts.com"
    assert scan_for_disclosure("Short sentences. No bullets. Uses dashes.") == ""
    # An @ that is not an address must not fire: the parse decides, not an eyeball.
    assert scan_for_disclosure("Signs off with a handle like @priya sometimes.") == ""


def test_a_disclosing_row_on_disk_is_skipped_rather_than_loaded(store_path: Path) -> None:
    """The refusal is a property of the ARTIFACT, not only of the write path."""
    save(store({}, _profile("external"), now=NOW), store_path)
    store_path.write_text(
        store_path.read_text(encoding="utf-8").replace(
            "First name and a comma.", "Writes to sam@acmeparts.com."
        ),
        encoding="utf-8",
    )
    assert load(store_path) == {}


# ---------------------------------------------------------------------------
# 3. Approval gates drafting
# ---------------------------------------------------------------------------


def test_an_unapproved_profile_is_not_usable_for_drafting(
    store_path: Path, routing: VoiceRouting
) -> None:
    save(store({}, _profile("external"), now=NOW), store_path)
    with pytest.raises(ProfileNotApprovedError) as caught:
        load_for_drafting(routing, "external", store_path)
    assert "has not been approved" in str(caught.value)


def test_the_refusal_names_the_approving_command(store_path: Path, routing: VoiceRouting) -> None:
    save(store({}, _profile("external"), now=NOW), store_path)
    with pytest.raises(ProfileNotApprovedError) as caught:
        load_for_drafting(routing, "external", store_path)
    assert APPROVE_COMMAND.replace("<voice>", "external") in str(caught.value)


def test_an_approved_profile_is_usable(store_path: Path, routing: VoiceRouting) -> None:
    profiles = approve(store({}, _profile("external"), now=NOW), "external", now=LATER)
    save(profiles, store_path)

    loaded = load_for_drafting(routing, "external", store_path)
    assert loaded.approved is True
    assert loaded.approved_at == LATER.isoformat()


def test_drafting_with_an_uninstalled_voice_is_refused(
    store_path: Path, routing: VoiceRouting
) -> None:
    with pytest.raises(ValueError, match="unknown voice"):
        load_for_drafting(routing, "legal", store_path)


def test_approving_an_absent_voice_is_refused() -> None:
    with pytest.raises(ProfileNotFoundError):
        approve({}, "sales", now=NOW)


# ---------------------------------------------------------------------------
# 4. resolve_voice selects the profile -- the internal/external split
# ---------------------------------------------------------------------------


def test_the_internal_external_split_selects_different_profiles(
    store_path: Path, routing: VoiceRouting
) -> None:
    """The routing table IS the profile selector. Nothing here re-decides it."""
    profiles: dict[str, VoiceProfile] = {}
    profiles = store(profiles, _profile("internal", formality="First names, no preamble."), now=NOW)
    profiles = store(profiles, _profile("external", formality="Contractions, direct."), now=NOW)
    profiles = approve(profiles, "internal", now=LATER)
    profiles = approve(profiles, "external", now=LATER)
    save(profiles, store_path)

    internal_voice = resolve_voice(routing, ["colleague@rtech-consulting.com"])
    external_voice = resolve_voice(routing, ["buyer@acmeparts.com"])
    assert internal_voice != external_voice

    internal = load_for_drafting(routing, internal_voice, store_path)
    external = load_for_drafting(routing, external_voice, store_path)
    assert internal.voice == "internal"
    assert external.voice == "external"
    assert internal.formality != external.formality


def test_a_sales_label_selects_the_sales_profile(store_path: Path, routing: VoiceRouting) -> None:
    profiles = store({}, _profile("sales", formality="Warmer, still direct."), now=NOW)
    save(approve(profiles, "sales", now=LATER), store_path)

    voice = resolve_voice(routing, ["buyer@acmeparts.com"], ["Prospect"])
    assert voice == "sales"
    assert load_for_drafting(routing, voice, store_path).voice == "sales"


def test_validate_voice_name_refuses_a_voice_the_installer_did_not_configure(
    routing: VoiceRouting,
) -> None:
    with pytest.raises(ValueError, match="installed voices are"):
        validate_voice_name(routing, "marketing")


# ---------------------------------------------------------------------------
# 5. Derived only -- the sample is not retained
# ---------------------------------------------------------------------------


def test_the_profile_shape_has_no_field_for_message_content() -> None:
    """A body cannot be stored because there is nowhere to put it."""
    fields = set(VoiceProfile.model_fields)
    assert not fields & {"body", "bodies", "messages", "sample", "recipients", "subjects"}


def test_an_unknown_field_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError):
        VoiceProfile.model_validate({"voice": "external", "body": "hello"})


# ---------------------------------------------------------------------------
# 6. greeting_policy -- mirror the inbound salutation, or keep today's fixed form
# ---------------------------------------------------------------------------


def test_greeting_policy_defaults_to_fixed_so_stored_profiles_stay_valid() -> None:
    """The default IS today's behaviour, which is what keeps every stored row loadable.

    A new required field would invalidate the whole existing store on read;
    a defaulted one leaves ``load`` returning exactly what it returned before.
    """
    assert VoiceProfile(voice="external").greeting_policy == "fixed"


def test_greeting_policy_accepts_mirror() -> None:
    assert VoiceProfile(voice="external", greeting_policy="mirror").greeting_policy == "mirror"


def test_greeting_policy_refuses_a_value_that_is_not_one_of_the_two() -> None:
    """A Literal, not a free string: a typo is refused rather than silently ignored."""
    with pytest.raises(ValueError):
        VoiceProfile(voice="external", greeting_policy="mirrored")


def test_greeting_policy_is_not_scanned_as_free_text() -> None:
    """It is a closed enum, not prose, so it is not a disclosure surface.

    Asserted on the scannable field list rather than by round-tripping a value,
    because the point is that the field never REACHES the scan.
    """
    scanned = {name for name, _ in VoiceProfile(voice="external")._scannable()}
    assert "greeting_policy" not in scanned


def test_a_profile_stored_without_greeting_policy_still_loads(tmp_path: Path) -> None:
    """The existing on-disk shape has no such key. It must keep loading."""
    store_path = tmp_path / "voice-profiles.json"
    store_path.write_text(
        json.dumps({
            "version": SCHEMA_VERSION,
            "profiles": {"external": {"voice": "external", "greeting": "first name"}},
        }),
        encoding="utf-8",
    )
    loaded = load(store_path)
    assert loaded["external"].greeting_policy == "fixed"
