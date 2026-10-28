"""Apps - the runnable surfaces, one package per entry point.

Everything under here is a way to START the connector; nothing under here is
the connector. ``cli`` is the operator's surface (auth, config, digest, watch,
reset, rules) and ``server`` is the host AI's surface (ten MCP tools over
stdio). Both call into ``services`` and ``providers`` and own no business
logic of their own, so a defect in what the product DOES is never fixed here.

Each app is a package with a ``main.py`` holding ``main()`` and a
``__main__.py`` so ``python -m inbox_zero_connector.apps.<name>`` runs it.
The installed console script in ``pyproject.toml`` points at
``apps.cli.main:main``; the CLI's default subcommand is ``serve``, which
hands off to ``apps.server.main``, so one binary reaches both surfaces.

Key Components:
    - cli: argparse command line. The deliberate home of interactive steps
      (OAuth bootstrap, init-config) that cannot run inside a tool call.
    - server: the MCP stdio server. Exactly ten tools, four mutating, every
      mutation receipted to the audit ledger.

Pattern/Convention:
    An app imports services and providers; services never import an app.
    Add a surface by adding a package with ``main.py`` + ``__main__.py`` and
    registering its entry point in ``pyproject.toml``. Do not add logic here
    that a second surface would need - that logic belongs in ``services``.

Example:
    >>> from inbox_zero_connector.apps.cli.main import build_parser
    >>> build_parser().prog
    'inbox-zero-connector'

See Also:
    - inbox_zero_connector.services: the business logic both apps drive.
    - inbox_zero_connector.config: the keystone both apps load first.
"""

from __future__ import annotations
