"""``init-config`` can set the classifier vendor switch without the wizard.

The tier install scripts are flag-driven (``examples/tier-*.sh``/``.ps1``), so
a scripted install needs a flag path to the one setting that decides where
mail text is sent for classification. The KEY never travels here: it lives in
``secrets.json`` only, and the flag only records that one is required.
"""

from __future__ import annotations

import json

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.config import config_path

BASE = "https://bifrost.example.test/v1"


def _run(*extra: str) -> int:
    return cli.main(["init-config", "--owner-domain", "example.com", "--force", *extra])


def test_classifier_flags_are_written_whole_and_mark_a_key_required() -> None:
    assert _run("--classifier-base-url", BASE, "--classifier-model", "qwen3-4b") == 0
    written = json.loads(config_path().read_text(encoding="utf-8"))
    assert written["classifier"]["base_url"] == BASE
    assert written["classifier"]["model"] == "qwen3-4b"
    assert written["classifier"]["requires_key"] is True
    assert "classifier_api_key" not in json.dumps(written)


def test_no_classifier_flags_writes_no_classifier_block() -> None:
    assert _run() == 0
    written = json.loads(config_path().read_text(encoding="utf-8"))
    assert "classifier" not in written


def test_model_alone_keeps_the_default_egress_unset() -> None:
    assert _run("--classifier-model", "qwen3-4b") == 0
    written = json.loads(config_path().read_text(encoding="utf-8"))
    assert written["classifier"]["base_url"] is None
    assert written["classifier"]["requires_key"] is False
