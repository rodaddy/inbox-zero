"""First run MINTS an install id, so a fresh unzip is namespaced before it authenticates.

WHY THIS FILE EXISTS (#171, second half)
    The first half of #171 made the keychain REFUSE an empty install id: an
    un-namespaced account name is not "no namespace", it is the namespace every
    legacy install on the machine already occupies, so an uninitialized copy
    would silently adopt whichever live token got there first. That refusal is
    correct and ``tests/test_install_id.py`` pins it.

    It also left a fresh self-installed copy with no way forward. Nothing minted
    an id, so the only installs that could ever authenticate were the custom
    zips ``build_custom.py`` stamps at build time -- and the refusal message
    says "run setup to initialize this install", which was not true, because
    ``setup`` wrote a config with no id in it. This file is the other half: the
    two installer surfaces mint an id on first run and PERSIST it, so the
    sentence the refusal prints is a sentence that works.

WHERE THE MINTED ID LANDS, AND WHY IT IS NOT THE INSTALLED CONFIG
    ``install_id()`` reads exactly two sources: ``INBOX_ZERO_INSTALL_ID`` and
    the ``install_id`` key of the committed layer-1 file at
    ``PROJECT_ROOT/config/connector-config.json``. It CANNOT read the installed
    per-user file, because that file's own directory is what the id resolves --
    reading it would be a cycle. So the mint writes layer 1, the same key and
    the same file ``build_custom.py`` stamps, and the tests here assert against
    that file rather than against ``config_path()``.

WHAT STABILITY MEANS HERE
    An id that changed on the second run would be worse than no id at all: the
    config directory, the data directory, and the keychain account would all
    move, so the install would lose its taxonomy, its ledger, and its stored
    token and look like a fresh one holding somebody's live mailbox. Every
    re-run assertion below is that property, not idempotence pedantry.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from inbox_zero_connector import config_paths as paths_module
from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.config import install_id
from inbox_zero_connector.config_paths import (
    INSTALL_ID_ENV,
    INSTALL_ID_PATTERN,
    reset_install_id_cache,
)

LAYER_ONE: tuple[str, str] = ("config", "connector-config.json")
"""The committed layer-1 path, relative to ``PROJECT_ROOT``. The mint's target."""

# The wizard's full answer script, in prompt order, copied from
# ``tests/apps/cli/test_setup.py`` so both files drive the same run.
WIZARD_ANSWERS: tuple[str, ...] = (
    # Account 1. The address domain is not a known consumer one, so the wizard
    # asks for the provider rather than inferring it.
    "owner@example.com",
    "google",  # provider, asked because example.com states nothing
    "",  # owner domain: accept the default taken from the address
    "",  # posture: accept the file_only default
    "n",  # drafts on this account
    "n",  # add another mailbox?
    "",  # sales labels: none
    "",  # classifier base URL: keep the default
    "",  # classifier model: accept the default
    "n",  # requires_key
    "",  # google client id: skip
    "",  # microsoft client id: skip
    "",  # tenant: accept the "common" default
)


@pytest.fixture(autouse=True)
def _fresh_install_id_cache() -> Iterator[None]:
    """The layer-1 read is cached per process, so every test starts from cold.

    The mint writes the file the cache is a snapshot of, so a stale cache would
    make a freshly-minted id invisible to ``install_id()`` and turn a real
    regression into a passing test.
    """
    reset_install_id_cache()
    yield
    reset_install_id_cache()


@pytest.fixture
def fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with NO committed layer-1 file and no ``INBOX_ZERO_INSTALL_ID``.

    This is the fresh-unzip state exactly: the self-installed copy that
    ``build_custom.py`` never stamped. Both halves matter -- the env var is
    dropped because it would supply an id the mint is supposed to create, and
    ``PROJECT_ROOT`` is repointed on ``config_paths`` because that is the module
    global the resolver actually reads.

    Returns:
        The fake ``PROJECT_ROOT``, so a test can read the file the mint wrote.
    """
    monkeypatch.delenv(INSTALL_ID_ENV, raising=False)
    root = tmp_path / "fresh-unzip"
    root.mkdir()
    monkeypatch.setattr(paths_module, "PROJECT_ROOT", root)
    monkeypatch.setattr("inbox_zero_connector.config.PROJECT_ROOT", root)
    return root


def _layer_one_id(root: Path) -> Any:
    """The ``install_id`` value the committed layer-1 file states, or ``None``.

    Reads the raw JSON rather than going through ``install_id()`` on purpose:
    these tests are about what reached DISK, and the resolver would answer from
    its cache or from an environment variable and hide an absent write.
    """
    target = root.joinpath(*LAYER_ONE)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8")).get("install_id")


def _run_setup(monkeypatch: pytest.MonkeyPatch) -> int:
    """Drive the interactive wizard end to end with the scripted answers."""
    answers = iter(WIZARD_ANSWERS)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("inbox_zero_connector.apps.cli.setup._refuse_without_a_tty", lambda: False)
    return cli.main(["setup"])


def _run_init_config(*extra: str) -> int:
    """Drive the flag-driven installer the tier scripts call."""
    return cli.main(["init-config", "--owner-domain", "example.com", "--force", *extra])


def test_init_config_mints_an_install_id_on_a_fresh_install(fresh_install: Path) -> None:
    """The scripted installer leaves the install namespaced. THE headline property."""
    assert _layer_one_id(fresh_install) is None
    assert _run_init_config() == 0
    minted = _layer_one_id(fresh_install)
    assert isinstance(minted, str)
    assert minted != ""
    assert INSTALL_ID_PATTERN.match(minted)


def test_setup_mints_an_install_id_on_a_fresh_install(
    fresh_install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard a human runs at a terminal mints the same way the flags do."""
    assert _run_setup(monkeypatch) == 0
    minted = _layer_one_id(fresh_install)
    assert isinstance(minted, str)
    assert minted != ""
    assert INSTALL_ID_PATTERN.match(minted)


def test_the_minted_id_is_what_the_resolver_reports(fresh_install: Path) -> None:
    """Persisted where ``install_id()`` reads, not merely written somewhere.

    The keychain builds its account name from ``install_id()``, so a mint that
    landed in a file the resolver does not read would leave the refusal firing
    on an install that had just been set up.
    """
    assert _run_init_config() == 0
    reset_install_id_cache()
    assert install_id() == _layer_one_id(fresh_install)
    assert install_id() != ""


def test_the_id_is_stable_across_reruns(fresh_install: Path) -> None:
    """A second installer pass must NOT re-mint.

    A changed id relocates the config directory, the data directory, and the
    keychain account in one step, so the install loses its taxonomy, its
    ledger, and its stored token. Re-running an idempotent installer is a
    normal thing an operator does; it must not orphan the mailbox.
    """
    assert _run_init_config() == 0
    first = _layer_one_id(fresh_install)
    reset_install_id_cache()
    assert _run_init_config() == 0
    assert _layer_one_id(fresh_install) == first


def test_init_config_never_overwrites_a_stamped_id(fresh_install: Path) -> None:
    """A custom zip's stamped id survives the flag-driven installer.

    ``build_custom.py`` writes the profile name into layer 1 at BUILD time, and
    that id is the managed install's identity. An installer that minted over it
    would move a live managed install off its own directories and token.
    """
    target = fresh_install.joinpath(*LAYER_ONE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"install_id": "sample-install"}), encoding="utf-8")
    assert _run_init_config() == 0
    assert _layer_one_id(fresh_install) == "sample-install"


def test_setup_never_overwrites_a_stamped_id(
    fresh_install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard leaves a stamped id alone too. Separate run, not a second pass.

    Driven on its own fresh install rather than after ``init-config``, because
    an already-written installed config makes the wizard open with an
    overwrite prompt and consume an answer the script does not carry -- which
    would fail here for a reason that has nothing to do with the id.
    """
    target = fresh_install.joinpath(*LAYER_ONE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"install_id": "sample-install"}), encoding="utf-8")
    assert _run_setup(monkeypatch) == 0
    assert _layer_one_id(fresh_install) == "sample-install"


def test_an_env_named_id_is_not_persisted_over(
    fresh_install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``INBOX_ZERO_INSTALL_ID`` already names this install, so nothing is minted.

    The environment is the higher layer everywhere else in this connector, and
    writing its value into the committed file would freeze a per-run override
    into the artifact -- the operator who unset the variable would then find the
    install still answering to it.
    """
    monkeypatch.setenv(INSTALL_ID_ENV, "from-env")
    assert _run_init_config() == 0
    assert _layer_one_id(fresh_install) is None
    reset_install_id_cache()
    assert install_id() == "from-env"


def test_the_mint_preserves_the_rest_of_layer_one(fresh_install: Path) -> None:
    """Minting edits ONE key. Layer 1 is a full config layer, not an id file.

    A rebuild-from-scratch write here would silently drop the shipped Microsoft
    client id and the classifier defaults every zip carries -- the same defect
    class ``_existing_payload`` exists to prevent one file over.
    """
    target = fresh_install.joinpath(*LAYER_ONE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"folder_prefix": "IZ/", "credentials": {"microsoft_client_id": "shipped"}}),
        encoding="utf-8",
    )
    assert _run_init_config() == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["folder_prefix"] == "IZ/"
    assert written["credentials"]["microsoft_client_id"] == "shipped"
    assert written["install_id"]
