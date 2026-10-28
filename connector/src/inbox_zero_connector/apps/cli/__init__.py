"""CLI - the operator's command line for the connector.

The command line exists for the steps a host AI cannot perform from inside a
tool call: an interactive OAuth consent (``auth``), writing the first config
and taxonomy (``init-config``, ``init-taxonomy``), reading what was granted
(``show-scopes``, ``paths``, ``list-folders``), and the unattended ``watch``
loop. ``serve`` is
the default subcommand and simply runs ``apps.server``.

``main.py`` is one argparse parser plus a flat dispatch table: one
``_cmd_*`` function per subcommand, each returning an exit code, none holding
logic a second caller would want - the work is delegated to ``services``.
``__main__.py`` makes ``python -m inbox_zero_connector.apps.cli`` equivalent
to the installed ``inbox-zero-connector`` script.

Key Components:
    - main: ``build_parser`` (the subcommand table), ``main`` (the console
      entry point), and the ``_cmd_*`` handlers.
    - desktop: ``cmd_desktop_config``, the ``desktop-config`` renderer that
      prints this install's Claude Desktop ``mcpServers`` entry -- split out
      of ``main`` for the code-line ceiling, CLI-only like everything here.
    - show_scopes: ``cmd_show_scopes``, the ``show-scopes`` renderer that
      prints this install's LIVE OAuth scope manifest -- split out of ``main``
      for the code-line ceiling, and the same stdout the diagnose bundle
      captures.
    - list_folders: ``cmd_list_folders``, the ``list-folders`` renderer that
      prints every container this install can ENUMERATE with the installed
      scope's coverage of each -- the answer to the watcher's listing WARN,
      where a folder present in the mailbox and absent here is mail refused
      before the scope is consulted (#198).
    - diagnose: ``cmd_diagnose``, the ``diagnose`` writer that packages the
      trace, ledger, watch state, config, taxonomy, scope manifest, digests,
      versions, and log tails into one content-free zip a beta tester mails
      back -- never secrets.json, a token, or mail content.
    - init_config: ``cmd_init_config`` + ``add_parser``, the flag-driven
      ``init-config`` writer (posture, drafts, routing, classifier vendor
      switch) -- split out of ``main`` for the code-line ceiling.
    - __main__: module-run shim, nothing else.

Pattern/Convention:
    Add a subcommand by adding a ``_cmd_*`` function, a parser row in
    ``build_parser`` (or an ``_add_*_parser`` helper when it has options),
    and a ``_dispatch`` row. Handlers print for the operator and return an
    int; they never write the ledger or touch a mail API directly.

Example:
    >>> from inbox_zero_connector.apps.cli.main import build_parser
    >>> build_parser().parse_args(["watch", "--once"]).once
    True

See Also:
    - inbox_zero_connector.apps.server: what ``serve`` runs.
    - inbox_zero_connector.services.auth: what ``auth`` and ``show-scopes``
      read and write.
"""

from __future__ import annotations
