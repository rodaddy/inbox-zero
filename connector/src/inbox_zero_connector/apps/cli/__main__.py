"""Run the connector CLI as a module: ``python -m inbox_zero_connector.apps.cli``.

``main()`` RETURNS its exit status rather than raising, so the return value
must reach ``SystemExit`` here exactly as the console script does
(``main.py``'s entrypoint) -- discarding it made the module form exit 0 on
every refusal the console script exits 1 on (#192), which silently voided
the ``watch --once`` exit-code contract for any wrapper using this form.
"""

from __future__ import annotations

from inbox_zero_connector.apps.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
