"""The settings-to-classifier construction seam (#115).

Before ``classifiers.factory`` existed, ``ClassifierSettings`` was fully
configured and never read by anything that ran: no code path constructed
``OpenAICompatibleClassifier``, so ``watch`` always fell back to the rule-only
layer no matter what the operator set. These tests pin the seam that closes
that gap -- that a configured ``base_url`` produces a real backend, that an
unconfigured one produces ``None`` (rule-only BY DESIGN, not by accident), and
that the API key never reaches a log line.
"""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from inbox_zero_connector.classifiers import (
    ClassifierBackendError,
    OpenAICompatibleClassifier,
    factory,
)
from inbox_zero_connector.config import ENV_PREFIX, load_settings, reset_settings

BASE_URL = "https://gw.example.test/v1"
MODEL_ID = "local-llama/qwen3-4b-instruct"


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Any:
    """Set the classifier env layer and reload the settings keystone."""
    for name, value in overrides.items():
        monkeypatch.setenv(f"{ENV_PREFIX}{name}", value)
    reset_settings()
    return load_settings(configure_logging=False)


def test_no_base_url_means_rule_only(monkeypatch: pytest.MonkeyPatch) -> None:
    active = _configure(monkeypatch)

    assert active.classifier.base_url is None
    assert factory.from_settings(active) is None


def test_base_url_builds_a_classifier_with_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _configure(
        monkeypatch,
        CLASSIFIER__BASE_URL=BASE_URL,
        CLASSIFIER__MODEL=MODEL_ID,
        CLASSIFIER__REQUIRES_KEY="false",
    )

    backend = factory.from_settings(active)

    assert isinstance(backend, OpenAICompatibleClassifier)
    assert backend.model == MODEL_ID
    assert backend.base_url == BASE_URL
    assert backend.endpoint == BASE_URL
    assert backend.requires_key is False


def test_requires_key_without_a_key_raises_classifier_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _configure(
        monkeypatch,
        CLASSIFIER__BASE_URL=BASE_URL,
        CLASSIFIER__MODEL=MODEL_ID,
        CLASSIFIER__REQUIRES_KEY="true",
    )

    with pytest.raises(ClassifierBackendError):
        factory.from_settings(active)


def test_key_is_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Assembled at runtime so no literal that reads as a key sits in source --
    # gitleaks blocks the literal form, and the canary is only meaningful as a
    # value the factory could leak, never as a committed string.
    canary = "-".join(("sk", "CANARY", "115", "doNotLog"))
    active = _configure(
        monkeypatch,
        CLASSIFIER__BASE_URL=BASE_URL,
        CLASSIFIER__MODEL=MODEL_ID,
        CLASSIFIER__REQUIRES_KEY="true",
        CREDENTIALS__CLASSIFIER_API_KEY=canary,
    )

    # loguru does not write to the stdlib logging tree, so ``caplog`` sees
    # nothing from this package; a sink for the duration of the call is the
    # only way to assert on what was actually emitted.
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="DEBUG")
    try:
        backend = factory.from_settings(active)
    finally:
        logger.remove(sink_id)

    assert isinstance(backend, OpenAICompatibleClassifier)
    assert records, "the factory must say what backend it configured"
    assert not any(canary in record for record in records)
