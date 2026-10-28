"""Shared scope fixtures: the named ``MailboxScope`` values every table probes.

NOT a conftest. These are plain module constants, not fixtures, and they are
specific to the mailbox-scope tables -- a root conftest would publish names like
``ALL_FOLDERS`` and ``INBOX_ONLY`` to every unrelated test module in the suite
for no gain. A leading-underscore module is not collected by pytest and is
imported by name, so each scope test file states its dependency out loud.

Split out of ``test_mailbox_scope.py`` on 2026-08-19, when that file exceeded
the repo's 500-code-line ceiling and had to be broken at its own
``INPUT-DOMAIN CHARACTERIZATION`` divider. Both halves import this.
"""

from __future__ import annotations

from pydantic import ValidationError as ValidationError

from inbox_zero_connector.config import (
    DEFAULT_SCOPES as DEFAULT_SCOPES,
)
from inbox_zero_connector.config import (
    MAX_SCOPE_FOLDERS as MAX_SCOPE_FOLDERS,
)
from inbox_zero_connector.config import (
    PROVIDER_NAMES,
    MailboxScope,
)
from inbox_zero_connector.config import (
    SCOPE_MODES as SCOPE_MODES,
)
from inbox_zero_connector.config import (
    ConfigError as ConfigError,
)
from inbox_zero_connector.config import (
    ConnectorConfig as ConnectorConfig,
)
from inbox_zero_connector.config import (
    default_config as default_config,
)
from inbox_zero_connector.config import (
    default_scope as default_scope,
)
from inbox_zero_connector.config import (
    load_config as load_config,
)
from inbox_zero_connector.config import (
    write_config as write_config,
)
from inbox_zero_connector.services.watch.scope import (
    NESTING_SEPARATOR as NESTING_SEPARATOR,
)

# ``NESTING_SEPARATOR`` is imported from its owning module rather than through
# the ``config`` re-export, because ``config`` does not re-export it: the flag
# it qualifies is a field on ``MailboxScope`` and no caller needs the constant
# to configure a scope. The other names above come through ``config`` because
# that is the surface the installer uses.

GOOGLE, MICROSOFT = PROVIDER_NAMES

# Scopes reused across the tables below. Named rather than rebuilt per row so a
# row reads as the QUESTION being asked, not as model construction.
INBOX_ONLY = MailboxScope()
ALL_FOLDERS = MailboxScope(mode="all_folders")
EXCEPT_MATTERS = MailboxScope(mode="all_except", excluded_folders=("Client Matters",))
ONLY_BILLING = MailboxScope(mode="only_listed", included_folders=("Billing",))
NESTED_EXCLUDE = MailboxScope(
    mode="all_except", excluded_folders=("Clients",), names_encode_nesting=True
)
NESTED_INCLUDE = MailboxScope(
    mode="only_listed", included_folders=("Clients",), names_encode_nesting=True
)
FLAT_EXCLUDE = MailboxScope(mode="all_except", excluded_folders=("Clients",))
FLAT_INCLUDE = MailboxScope(mode="only_listed", included_folders=("Clients",))
