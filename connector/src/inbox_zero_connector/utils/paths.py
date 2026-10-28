"""Filesystem locations for config and data. Re-exported from the keystone.

Everything lives under the OS-standard per-user directories via platformdirs.
Nothing is written beside the source tree, and nothing token-like is written
to any of these paths -- credentials live in the OS keychain only (see
``auth``).

WHY THESE FUNCTIONS LIVE IN ``config`` AND NOT HERE
    Resolving a path means reading ``INBOX_ZERO_CONFIG_DIR`` /
    ``INBOX_ZERO_DATA_DIR``, and the keystone LAW
    (``_DOCS/STANDARDS-python.md`` ## config.py -- the keystone) is that
    ``config.py`` is the ONLY module that reads the environment. Keeping the
    ``os.environ`` call here and the rest of configuration there would put the
    rule half in force, which is how it stops being checkable.

    This module stays as the import site every other module already uses, so
    the boundary moved without every caller moving with it.

THE ONE SANCTIONED EXCEPTION TO THE KEYSTONE LAW
    ``config.install_id()`` reads the ``install_id`` key directly out of
    ``config/connector-config.json`` instead of going through ``Settings``.
    That is not a leak in the rule, it is the rule's one unavoidable edge:
    ``Settings`` loads layered files whose LOCATION this value decides, so
    resolving it through ``settings()`` would be a cycle. The read is a single
    key, from layer 1 only, with a safe ``""`` fallback on a missing or
    malformed file, and it lives inside ``config.py`` -- so the law that
    ``config.py`` is the only module touching the environment and the config
    files still holds, byte for byte. Do not copy the pattern anywhere else.
"""

from __future__ import annotations

from inbox_zero_connector.config import (
    APP_NAME as APP_NAME,
)
from inbox_zero_connector.config import (
    config_dir as config_dir,
)
from inbox_zero_connector.config import (
    config_path as config_path,
)
from inbox_zero_connector.config import (
    data_dir as data_dir,
)
from inbox_zero_connector.config import (
    install_id as install_id,
)
from inbox_zero_connector.config import (
    ledger_path as ledger_path,
)
from inbox_zero_connector.config import (
    taxonomy_path as taxonomy_path,
)
from inbox_zero_connector.config import (
    voice_profiles_path as voice_profiles_path,
)

APP_AUTHOR = "rTech"
