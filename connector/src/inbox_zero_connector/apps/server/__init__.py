"""Server - the MCP stdio surface the host AI operates through.

Exactly ten tools, and the count is the contract: six read, four mutate
(``file_message``, ``record_correction``, ``mark_important``,
``create_draft``), and every mutation writes a receipt to the audit ledger
before the tool returns. There is no send, delete, or forward tool because
the provider interface has no such method - the absence is the guarantee,
not a guard in this package.

stdout is the JSON-RPC channel, so nothing here may print; all logging goes
through ``utils.logging_config`` to stderr with content carried as bound
fields so redaction can match on a field name. ``main.py`` holds the tool
functions and the lazily built provider/config/taxonomy singletons;
``__main__.py`` makes ``python -m inbox_zero_connector.apps.server`` run it.

Key Components:
    - main: the ``mcp`` server object, the ten ``@mcp.tool`` functions, the
      ``config``/``provider``/``taxonomy``/``owner_address`` accessors, and
      ``reset_state`` for tests.
    - __main__: module-run shim, nothing else.

Pattern/Convention:
    A tool validates its arguments, calls one service or provider method,
    and returns a plain dict. Adding a tool is a security-posture change:
    update the enforcement table in the connector README and the
    ``test_server_tools`` count before the code. Never interpolate mail
    content into a log message - ``find_content_interpolations`` fails the
    build on it.

Example:
    >>> from inbox_zero_connector.apps.server import main as server
    >>> server.__name__
    'inbox_zero_connector.apps.server.main'

See Also:
    - inbox_zero_connector.providers.base: the interface whose absences bound
      these tools.
    - inbox_zero_connector.services.audit.ledger: where the four mutating
      tools leave receipts.
"""

from __future__ import annotations
