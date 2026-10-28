"""The voice routing table and the draft posture flag.

``resolve_voice`` is a PURE function, so these tests are the whole story about
how a voice gets chosen -- there is no other code path, no model call, and
nothing a message body can reach. That is the property the spec asked for
("script-decided, never AI-decided"), and a pure function is what makes it
provable rather than asserted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from loguru import logger

from inbox_zero_connector.config import (
    DEFAULT_FOLDER_PREFIX,
    DEFAULT_VOICES,
    ENV_PREFIX,
    MAX_FOLDER_PREFIX_CHARS,
    ConfigError,
    ConnectorConfig,
    UnknownVoiceError,
    VoiceRouting,
    default_config,
    load_config,
    load_settings,
    reset_settings,
    write_config,
)
from inbox_zero_connector.voice_routing import (
    extract_domain,
    normalize_address,
    resolve_voice,
)

OWNER = "example.com"


@pytest.fixture
def routing() -> VoiceRouting:
    return VoiceRouting(owner_domain=OWNER, sales_labels=("Prospects", "Leads"))


# ---------------------------------------------------------------------------
# Rule 1: same-domain -> internal
# ---------------------------------------------------------------------------


def test_all_same_domain_recipients_route_to_internal(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, ["a@example.com", "b@example.com"]) == "internal"


def test_domain_matching_is_case_insensitive(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, ["A@Example.COM"]) == "internal"


def test_a_display_name_form_still_parses(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, ["Ann Example <ann@example.com>"]) == "internal"


def test_a_trailing_dot_fqdn_is_the_same_domain(routing: VoiceRouting) -> None:
    """example.com. and example.com are one identity, not two."""
    assert resolve_voice(routing, ["ann@example.com."]) == "internal"


def test_a_lookalike_subdomain_is_not_internal(routing: VoiceRouting) -> None:
    """evil-example.com and mail.example.com are NOT the owner's domain.

    Matching is exact on purpose. A suffix match would make
    ``notexample.com`` internal, and a prefix match would make
    ``example.com.attacker.test`` internal -- both are how a phisher gets the
    owner's unguarded register.
    """
    assert resolve_voice(routing, ["a@evil-example.com"]) == "external"
    assert resolve_voice(routing, ["a@notexample.com"]) == "external"
    assert resolve_voice(routing, ["a@example.com.attacker.test"]) == "external"
    assert resolve_voice(routing, ["a@mail.example.com"]) == "external"


# ---------------------------------------------------------------------------
# Rule 2: sales labels
# ---------------------------------------------------------------------------


def test_a_sales_label_with_an_outside_recipient_routes_to_sales(
    routing: VoiceRouting,
) -> None:
    assert resolve_voice(routing, ["lead@other.example"], ["Prospects"]) == "sales"


def test_sales_label_matching_is_case_insensitive(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, ["lead@other.example"], ["prospects"]) == "sales"


def test_an_unconfigured_label_does_not_route_to_sales(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, ["lead@other.example"], ["Random"]) == "external"


def test_no_sales_labels_configured_means_the_rule_never_fires() -> None:
    plain = VoiceRouting(owner_domain=OWNER)
    assert resolve_voice(plain, ["lead@other.example"], ["Prospects"]) == "external"


def test_an_all_internal_thread_stays_internal_despite_a_sales_label(
    routing: VoiceRouting,
) -> None:
    """Rule 1 wins on a genuine all-internal thread.

    The owner talking to their own colleague ABOUT a deal is not the owner
    talking to a prospect. Routing that to the sales voice would put pitch
    language in front of a co-worker.
    """
    assert resolve_voice(routing, ["colleague@example.com"], ["Prospects"]) == "internal"


# ---------------------------------------------------------------------------
# Rule 3 / ambiguity: everything else is external
# ---------------------------------------------------------------------------


def test_an_outside_recipient_routes_to_external(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, ["client@other.example"]) == "external"


def test_a_mixed_thread_routes_to_external(routing: VoiceRouting) -> None:
    """The decisive ambiguity case, and the reason the default is external.

    One colleague plus one outsider is EXTERNAL. Treating it as internal would
    leak the owner's unguarded register to the outsider.
    """
    assert resolve_voice(routing, ["colleague@example.com", "client@other.example"]) == "external"


def test_no_recipients_routes_to_external(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, []) == "external"


def test_an_unparseable_recipient_routes_to_external(routing: VoiceRouting) -> None:
    assert resolve_voice(routing, ["not-an-address"]) == "external"
    assert resolve_voice(routing, ["a@b@c"]) == "external"


def test_one_unparseable_recipient_poisons_an_otherwise_internal_thread(
    routing: VoiceRouting,
) -> None:
    """Ambiguity is ambiguity even when most of the thread looks internal."""
    assert resolve_voice(routing, ["colleague@example.com", "garbage"]) == "external"


def test_an_empty_owner_domain_routes_everything_to_external() -> None:
    """A misconfigured owner domain must not make everybody internal."""
    broken = VoiceRouting(owner_domain="")
    assert resolve_voice(broken, ["a@example.com"]) == "external"


def test_resolve_voice_ignores_message_content_entirely(routing: VoiceRouting) -> None:
    """It has no parameter for a body, so no injection can reach it.

    Stated as a signature assertion rather than as prose, because that is the
    form that survives a refactor.
    """
    import inspect

    parameters = set(inspect.signature(resolve_voice).parameters)
    assert parameters == {"routing", "recipients", "thread_labels"}
    for banned in ("body", "bodies", "content", "text", "message", "subject"):
        assert banned not in parameters


def test_the_routed_voice_is_always_an_installed_voice(routing: VoiceRouting) -> None:
    """Total function: every input produces a legal voice or raises. No None."""
    cases: list[tuple[list[str], list[str]]] = [
        ([], []),
        (["a@example.com"], []),
        (["a@other.example"], []),
        (["a@other.example"], ["Prospects"]),
        (["a@example.com", "b@other.example"], ["Prospects"]),
        (["garbage"], ["Leads"]),
    ]
    for recipients, labels in cases:
        assert resolve_voice(routing, recipients, labels) in routing.voices


# ---------------------------------------------------------------------------
# Voice validation
# ---------------------------------------------------------------------------


def test_validate_voice_accepts_an_installed_voice(routing: VoiceRouting) -> None:
    assert routing.validate_voice("sales") == "sales"


def test_validate_voice_rejects_anything_else(routing: VoiceRouting) -> None:
    with pytest.raises(UnknownVoiceError) as caught:
        routing.validate_voice("ceo")
    assert caught.value.allowed == DEFAULT_VOICES
    assert "cannot be extended at runtime" in str(caught.value)


def test_voice_matching_is_case_sensitive(routing: VoiceRouting) -> None:
    """Same rule as the taxonomy: exactness keeps providers behaving alike."""
    with pytest.raises(UnknownVoiceError):
        routing.validate_voice("Internal")


# ---------------------------------------------------------------------------
# Drafts posture
# ---------------------------------------------------------------------------


def test_the_default_config_has_drafts_off_everywhere() -> None:
    """The safe default. Anything else would enable a send-capable scope silently."""
    config = default_config()
    assert config.drafts_on("google") is False
    assert config.drafts_on("microsoft") is False
    assert config.routing is None


def test_an_unknown_provider_is_drafts_off() -> None:
    assert default_config().drafts_on("yahoo") is False


def test_require_routing_raises_when_none_is_installed() -> None:
    with pytest.raises(ConfigError, match="No voice routing"):
        ConnectorConfig().require_routing()


# ---------------------------------------------------------------------------
# Load / write round trip and validation
# ---------------------------------------------------------------------------


def test_config_round_trips(tmp_path: Path, routing: VoiceRouting) -> None:
    target = tmp_path / "connector-config.json"
    written = ConnectorConfig(drafts_enabled={"google": True, "microsoft": False}, routing=routing)
    write_config(written, target)
    loaded = load_config(target)
    assert loaded.drafts_on("google") is True
    assert loaded.drafts_on("microsoft") is False
    assert loaded.require_routing() == routing


def test_a_missing_config_loads_the_safe_default(tmp_path: Path) -> None:
    """Absence must narrow behaviour, never widen it -- unlike the taxonomy."""
    loaded = load_config(tmp_path / "absent.json")
    assert loaded.drafts_on("google") is False
    assert loaded.routing is None


def _write(target: Path, payload: object) -> Path:
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_a_truthy_string_does_not_enable_drafts(tmp_path: Path) -> None:
    """The one type coercion that must never happen quietly.

    ``bool("false")`` is True, so accepting a string here would let a config
    typo request a send-capable Gmail scope.
    """
    target = _write(tmp_path / "c.json", {"drafts_enabled": {"google": "false"}})
    with pytest.raises(ConfigError, match="must be true or false"):
        load_config(target)


def test_an_unknown_provider_in_the_config_is_rejected(tmp_path: Path) -> None:
    target = _write(tmp_path / "c.json", {"drafts_enabled": {"yahoo": True}})
    with pytest.raises(ConfigError, match="unknown provider"):
        load_config(target)


def test_routing_without_an_owner_domain_is_rejected(tmp_path: Path) -> None:
    target = _write(tmp_path / "c.json", {"voice_routing": {"voices": ["internal"]}})
    with pytest.raises(ConfigError, match="owner_domain"):
        load_config(target)


def test_a_role_naming_an_uninstalled_voice_is_rejected_at_load(tmp_path: Path) -> None:
    """Fail at install, not on the first external draft in front of a customer."""
    target = _write(
        tmp_path / "c.json",
        {
            "voice_routing": {
                "owner_domain": OWNER,
                "voices": ["internal", "external", "sales"],
                "default_voice": "extenral",
            }
        },
    )
    with pytest.raises(ConfigError, match="default_voice"):
        load_config(target)


def test_every_named_role_is_checked_not_just_the_default(tmp_path: Path) -> None:
    """internal_voice and sales_voice get the same load-time check."""
    for role in ("internal_voice", "sales_voice"):
        target = _write(
            tmp_path / f"{role}.json",
            {
                "voice_routing": {
                    "owner_domain": OWNER,
                    "voices": ["internal", "external", "sales"],
                    role: "typo",
                }
            },
        )
        with pytest.raises(ConfigError, match=role):
            load_config(target)


def test_duplicate_voices_are_rejected(tmp_path: Path) -> None:
    target = _write(
        tmp_path / "c.json",
        {"voice_routing": {"owner_domain": OWNER, "voices": ["internal", "internal"]}},
    )
    with pytest.raises(ConfigError, match="duplicate voice"):
        load_config(target)


def test_an_empty_voice_list_is_rejected(tmp_path: Path) -> None:
    target = _write(tmp_path / "c.json", {"voice_routing": {"owner_domain": OWNER, "voices": []}})
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(target)


def test_too_many_voices_are_rejected(tmp_path: Path) -> None:
    target = _write(
        tmp_path / "c.json",
        {"voice_routing": {"owner_domain": OWNER, "voices": [f"v{i}" for i in range(99)]}},
    )
    with pytest.raises(ConfigError, match="limit is"):
        load_config(target)


def test_invalid_json_is_reported_not_ignored(tmp_path: Path) -> None:
    target = tmp_path / "c.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(target)


def test_an_installer_can_extend_the_voice_list(tmp_path: Path) -> None:
    """Spec: three by default, owner-extensible."""
    target = _write(
        tmp_path / "c.json",
        {
            "voice_routing": {
                "owner_domain": OWNER,
                "voices": ["internal", "external", "sales", "board"],
            }
        },
    )
    assert load_config(target).require_routing().voices == (
        "internal",
        "external",
        "sales",
        "board",
    )


# ---------------------------------------------------------------------------
# Address parsing helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ann@example.com", "example.com"),
        ("Ann Example <ann@example.com>", "example.com"),
        ('"Example, Ann" <ann@EXAMPLE.com>', "example.com"),
        ("ann@example.com.", "example.com"),
        ("garbage", ""),
        ("", ""),
        ("a@b@c", ""),
    ],
)
def test_extract_domain(raw: str, expected: str) -> None:
    assert extract_domain(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ann Example <Ann@Example.com>", "ann@example.com"),
        ("ann@example.com", "ann@example.com"),
        ("garbage", ""),
    ],
)
def test_normalize_address(raw: str, expected: str) -> None:
    assert normalize_address(raw) == expected


# ---------------------------------------------------------------------------
# folder_prefix -- the string both adapters paste in front of a taxonomy
# folder name. Lives here because config.py is the only module permitted to
# read the environment for it.
# ---------------------------------------------------------------------------


def test_the_default_prefix_is_empty() -> None:
    """A default of "" is a PRODUCT decision, not an omission.

    Asserted on the loaded settings and not only on the constant, because the
    value that matters is what an operator who configured nothing actually
    gets after the JSON layer and the environment have had their turn.
    """
    assert DEFAULT_FOLDER_PREFIX == ""
    assert load_settings(configure_logging=False).folder_prefix == ""


def test_the_environment_sets_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """``INBOX_ZERO_FOLDER_PREFIX`` is the operator's override."""
    monkeypatch.setenv(f"{ENV_PREFIX}FOLDER_PREFIX", "InboxZero-")
    reset_settings()
    assert load_settings(configure_logging=False).folder_prefix == "InboxZero-"


def test_the_installed_file_sets_the_prefix(tmp_path: Path) -> None:
    """The installer can persist it, same as any other decision it records."""
    target = tmp_path / "connector-config.json"
    target.write_text(json.dumps({"folder_prefix": "Filed/"}), encoding="utf-8")
    assert load_settings(target, configure_logging=False).folder_prefix == "Filed/"


def test_the_environment_outranks_the_installed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary precedence, unlike ``drafts_enabled``.

    The prefix is a PREFERENCE -- it renames folders, it does not widen a
    scope -- so the operator's environment wins over a stale installed file
    rather than failing the startup the way a posture disagreement does.
    """
    target = tmp_path / "connector-config.json"
    target.write_text(json.dumps({"folder_prefix": "Filed/"}), encoding="utf-8")
    monkeypatch.setenv(f"{ENV_PREFIX}FOLDER_PREFIX", "InboxZero-")
    reset_settings()
    assert load_settings(target, configure_logging=False).folder_prefix == "InboxZero-"


def test_the_prefix_is_not_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A separator is the point of a prefix, and a space is a legal separator.

    Normalising whitespace here would silently rename every folder the
    operator asked for, which is the failure this setting exists to end.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}FOLDER_PREFIX", "Inbox Zero ")
    reset_settings()
    assert load_settings(configure_logging=False).folder_prefix == "Inbox Zero "


def test_an_oversized_prefix_is_refused_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling fires at startup, not at the first filing.

    A prefix that long is a corrupted or hand-edited config, and discovering it
    when a message is being filed means discovering it after folders have
    already been created in the owner's mailbox.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}FOLDER_PREFIX", "x" * (MAX_FOLDER_PREFIX_CHARS + 1))
    reset_settings()
    with pytest.raises(ConfigError, match="folder_prefix"):
        load_settings(configure_logging=False)


# ---------------------------------------------------------------------------
# The classifier section -- the vendor switch, and the one configured egress.
# ---------------------------------------------------------------------------


def test_the_classifier_defaults_name_no_endpoint() -> None:
    """A fresh install ships NO model endpoint, and that is the design.

    A hardcoded base URL would bake one deployment's infrastructure into every
    install. ``None`` leaves the SDK on its own documented default, so the cost
    of forgetting to configure this is a 401 from a known vendor rather than a
    silent connection to an unexpected host.
    """
    settings = load_settings(configure_logging=False)
    assert settings.classifier.base_url is None
    assert settings.classifier.requires_key is False
    assert settings.classifier.model == "Qwen3-4B-Instruct-2507-4bit"


def test_the_classifier_fields_load_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three halves of the vendor switch are operator-settable.

    Pointing this deployment at a different model backend must be
    configuration and nothing else -- no code change, no second class.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}CLASSIFIER__BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv(f"{ENV_PREFIX}CLASSIFIER__MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv(f"{ENV_PREFIX}CLASSIFIER__REQUIRES_KEY", "true")
    reset_settings()

    classifier = load_settings(configure_logging=False).classifier
    assert classifier.base_url == "https://openrouter.ai/api/v1"
    assert classifier.model == "openai/gpt-4o-mini"
    assert classifier.requires_key is True


def test_the_classifier_key_loads_as_a_secret_that_never_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is SecretStr, so no ordinary serialisation can leak it.

    THE POINT: a plain ``str`` prints in full from ``repr()``, from
    ``model_dump_json()``, and from any exception quoting the settings object.
    All three end up in a log file and none is checked by hand. The real value
    is still reachable through the one explicit accessor.
    """
    # Bound to `planted_value`, not `secret`: ruff S105 keys off the NAME, and
    # suppressing a hardcoded-credential rule to keep a prettier fixture would
    # trade a real guard for cosmetics. The test asserts an exact string never
    # appears in any serialisation; the name it is bound to is irrelevant.
    planted_value = "classifier-key-that-must-never-print"
    monkeypatch.setenv(f"{ENV_PREFIX}CREDENTIALS__CLASSIFIER_API_KEY", planted_value)
    reset_settings()

    settings = load_settings(configure_logging=False)
    key = settings.credentials.classifier_api_key
    assert key is not None
    assert key.get_secret_value() == planted_value

    assert planted_value not in repr(settings)
    assert planted_value not in str(settings)
    assert planted_value not in str(settings.model_dump())
    assert planted_value not in settings.model_dump_json()


def test_the_classifier_key_is_absent_rather_than_empty_by_default() -> None:
    """Unset is ``None``, not ``""``.

    ``requires_key`` states whether a credential is mandatory; the emptiness of
    this field does not, and conflating the two would put that decision in two
    places.
    """
    assert load_settings(configure_logging=False).credentials.classifier_api_key is None


# ---------------------------------------------------------------------------
# The client posture -- a closed enum, defaulting narrow, announced when moot.
# ---------------------------------------------------------------------------


def test_the_default_posture_is_file_only_everywhere_it_is_decided() -> None:
    """Both the field default and ``default_config`` say the narrow thing.

    Two assertions rather than one because they are two different decisions
    that could drift: an installer who constructs ``ConnectorConfig`` directly
    never touches ``default_config``, so a safe factory does not make a safe
    field.
    """
    from inbox_zero_connector.config import Posture

    assert default_config().posture is Posture.FILE_ONLY
    assert ConnectorConfig().posture is Posture.FILE_ONLY


def test_a_hand_typed_posture_value_is_a_load_error_not_a_silent_ship() -> None:
    """The whole reason it is a ``StrEnum`` -- ``watcher.RoundState``'s rule.

    A plain string field would take ``"file_read_drafts"`` and every
    ``is FILE_READ_DRAFT`` comparison downstream would quietly read False,
    which looks exactly like a deployment that asked for file_only.
    """
    with pytest.raises(ValueError):
        ConnectorConfig(posture="file_read_drafts")  # type: ignore[arg-type]


def test_posture_survives_a_write_read_round_trip() -> None:
    """The installer writes it; the connector must read back what was written.

    A posture that is written and then defaulted away on load is worse than
    one that was never written, because the config file a human reads would
    disagree with what the connector does -- the exact failure
    ``_env_may_not_flip_the_posture`` exists to refuse for the drafts flag.
    """
    from inbox_zero_connector.config import Posture

    written = write_config(
        ConnectorConfig(
            drafts_enabled={"google": True, "microsoft": True},
            posture=Posture.FILE_READ_DRAFT,
        )
    )
    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["posture"] == "file_read_draft"

    reset_settings()
    assert load_config().posture is Posture.FILE_READ_DRAFT


def test_file_read_draft_with_drafts_off_announces_itself_loudly_at_load(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator asked for a capability and will not get it. Say so.

    A WARNING rather than a raise, because posture and ``drafts_enabled`` do
    not contradict -- they compose, in one direction, and the composed answer
    is the safe one. What it must not be is SILENT, so the message is asserted
    to name both fields, the outcome, and the resolving command.
    """
    from inbox_zero_connector.config import Posture

    write_config(
        ConnectorConfig(
            drafts_enabled={"google": False, "microsoft": False},
            posture=Posture.FILE_READ_DRAFT,
        )
    )
    reset_settings()

    caplog.set_level(logging.WARNING)
    sink_id = logger.add(caplog.handler, level="WARNING", format="{message}")
    try:
        settings = load_settings(configure_logging=False)
    finally:
        logger.remove(sink_id)

    assert settings.connector_config().drafts_allowed("google") is False
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "file_read_draft" in message
    assert "drafts_enabled.google is false" in message
    assert "NO DRAFTS" in message
    assert "init-config --force --enable-google-drafts" in message


def test_a_consistent_posture_announces_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The negative control the warning above needs to mean anything.

    Without it, a validator that warned on EVERY load would pass the assertion
    above while telling the operator nothing, because a message that always
    appears carries no information.
    """
    from inbox_zero_connector.config import Posture

    write_config(
        ConnectorConfig(
            drafts_enabled={"google": True, "microsoft": True},
            posture=Posture.FILE_READ_DRAFT,
        )
    )
    reset_settings()

    caplog.set_level(logging.WARNING)
    sink_id = logger.add(caplog.handler, level="WARNING", format="{message}")
    try:
        load_settings(configure_logging=False)
    finally:
        logger.remove(sink_id)

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "NO DRAFTS" not in rendered
