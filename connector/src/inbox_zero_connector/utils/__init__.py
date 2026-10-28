"""Utils - the shared floor every other module in this package stands on.

``_DOCS/STANDARDS-python.md`` (``### `utils/` is the shared floor``) states the
rule these three modules were already obeying without a home: anything used by
more than one module goes here, and every module imports it from here. The
failure that prevents is the reach-across import -- a module pulling a helper
out of whatever module it happened to be written in first, which builds a
dependency graph nobody designed and turns a small refactor into a chain of
import errors. Until this directory existed, ``logging_config``, ``paths``, and
``sections`` sat flat at the package root doing exactly that job, and every
caller reached across to wherever each one landed.

The directional rule is what makes the floor a floor: things here are imported
BY the application modules and import nothing back from them. ``providers``,
``services``, and ``apps`` all depend on this package; an edge
pointing the other way is a cycle, and it is the shape this directory exists to
prevent. The one seam that looks like an exception is ``paths``, which re-exports
its resolvers from ``config``. That is deliberate and is documented in that
module: resolving a path means reading an environment variable, and the keystone
LAW is that ``config.py`` is the only module permitted to do so. ``paths`` stays
as the import site so the boundary could move without every caller moving too.

``sections`` earns its own three-line module for the same structural reason.
``config`` is the keystone and ``services.watch.scope`` is a section it
re-exports, so
both need the ``Section`` base and neither can own it without importing the
other. Pulling the base down to the floor makes that dependency a tree.

Key Components:
    - logging_config: owns every logging sink in the process -- nothing else
      calls ``logger.add()`` or ``logger.remove()``. Two rules are enforced
      rather than documented: the console sink is on **stderr**
      unconditionally, because stdout is the MCP JSON-RPC channel and one
      stray byte desynchronises the transport; and content travels as a BOUND
      FIELD, never as message prose, because redaction matches on a field's
      NAME. Ships the redaction pass, the sensitive-field table, the
      ``ProviderCall``/``ToolCall`` five-point wrappers, and
      ``find_content_interpolations``, an AST scan over every ``*.py`` in the
      package that fails the build when a call site interpolates content into
      a message string.
    - paths: the OS-standard per-user config and data locations, via
      platformdirs. Nothing is written beside the source tree, and nothing
      token-like is written to any of these paths -- credentials live in the
      OS keychain only (see ``services.auth.keychain``). The resolvers themselves are defined in
      ``config``; this module is their import site.
    - atomic_write: ``write_text_atomic`` - tmp sibling, fsync, rename. Every
      whole-file store writes through it; the ledger (append-only) does not.
    - observed: ``logged``, the five-point flow decorator (entry/exit/duration at
      DEBUG, failure at ERROR with traceback, re-raise). Decorate I/O boundaries
      and whole units of work; identifiers still go in explicit logger calls.
    - sections: ``Section``, the frozen pydantic base every settings section
      inherits. ``extra="forbid"`` turns a typo into a startup error instead
      of a silently dropped key -- and in this package one of those keys
      decides whether a send-capable Gmail scope is requested.

Pattern/Convention:
    Import from the package, not from the module inside it::

        from inbox_zero_connector.utils import data_dir, ToolCall

    The re-exports below are the public surface, mirroring how
    ``providers/__init__`` re-exports from ``base`` and ``factory``. Reaching
    past this ``__init__`` to ``inbox_zero_connector.utils.paths`` is the
    reach-across import the shared-floor rule exists to stop.

    Add a helper by adding a module here and one re-export line below. The bar
    for entry is that TWO modules need it -- a helper with a single caller
    belongs with its caller, where a reader finds it without a hop. Four lines
    is not too small to live here if two modules use it; one caller is too few
    no matter how large it is.

    ``observed`` is the first cross-cutting concern to land here after the three
    relocated helpers; the directory is the home for what every module touches,
    not only the helpers that needed moving first.

    THE PROMOTION LADDER (Rico, 2026-08-20) decides WHICH floor a helper
    lands on, and this package is the top rung, not the default one:

        1. used by ONE module            -> stays beside its caller
        2. used by 2+ siblings in a pkg  -> that package's own module
                                            (``providers/retry.py`` is the
                                            standing example)
        3. used across packages, or
           domain-neutral                -> here, root ``utils/``
        4. a pure data shape             -> root ``models/`` (``models.mail``
                                            holds Message and Container; add a
                                            shape only for a second consumer)

    Never promote above actual usage. Duplication is the trigger for a
    promotion, never the excuse for a second copy.

Example:
    >>> from inbox_zero_connector.utils import ToolCall, data_dir
    >>> with ToolCall("list_messages", folder="Action"):
    ...     path = data_dir()

See Also:
    - inbox_zero_connector.config: the keystone, and the only module permitted
      to read the environment. Owns the path resolvers ``paths`` re-exports.
    - inbox_zero_connector.services.auth.keychain: why no credential is ever written to a path
      named here.
"""

from __future__ import annotations

from inbox_zero_connector.utils.atomic_write import write_text_atomic as write_text_atomic

# Re-exported so callers import from the floor itself --
# ``from inbox_zero_connector.utils import data_dir`` -- rather than reaching
# past this module into ``inbox_zero_connector.utils.paths``. The ``as`` on each
# name is PEP 484's explicit re-export marker, which is what tells a type
# checker and ruff that the name is public surface rather than an unused
# import. It is a property of the import itself, so a name cannot go stale the
# way a separate ``__all__`` list does.
from inbox_zero_connector.utils.logging_config import (
    ContentInterpolation as ContentInterpolation,
)
from inbox_zero_connector.utils.logging_config import (
    ProviderCall as ProviderCall,
)
from inbox_zero_connector.utils.logging_config import (
    ToolCall as ToolCall,
)
from inbox_zero_connector.utils.logging_config import (
    find_content_interpolations as find_content_interpolations,
)
from inbox_zero_connector.utils.logging_config import (
    install_crash_handler as install_crash_handler,
)
from inbox_zero_connector.utils.logging_config import (
    is_sensitive_field as is_sensitive_field,
)
from inbox_zero_connector.utils.logging_config import (
    new_correlation_id as new_correlation_id,
)
from inbox_zero_connector.utils.logging_config import (
    setup as setup,
)
from inbox_zero_connector.utils.observed import logged as logged

# Deferred on purpose -- see the note above ``__getattr__`` at the end of this
# module. ``utils.paths`` re-exports from ``config``, and ``config`` imports
# ``services.watch.scope``, which imports THIS package. Importing paths eagerly here
# closes that loop at import time and breaks every cold entry point.
from inbox_zero_connector.utils.sections import Section as Section

# ruff PLC0414/`non-empty-init-module`: the ``__getattr__`` below IS a
# re-export -- a deferred one. Doing it eagerly closes an import cycle
# (see the docstring on ``__getattr__``), so the deferral is required,
# not a stylistic choice.
_PATHS_EXPORTS = frozenset({
    "APP_AUTHOR",
    "APP_NAME",
    "config_dir",
    "config_path",
    "data_dir",
    "ledger_path",
    "taxonomy_path",
})


def __getattr__(name: str) -> object:
    """Resolve path helpers on first use rather than at import time (PEP 562).

    ``utils.paths`` re-exports from ``config``, and ``config`` imports
    ``services.watch.scope``, which imports this package -- a cycle. Binding
    these names eagerly made ``import inbox_zero_connector.config``,
    ``... .apps.cli``, ``... .services.watch.scope`` and the shipped console
    script all die with
    ``ImportError: cannot import name 'APP_NAME' from partially initialized
    module``. Deferring the lookup to attribute access breaks the import-time
    edge while keeping ``from inbox_zero_connector.utils import data_dir``
    working for callers, which is what the docstring above promises.

    ``tests/apps/cli/test_cold_import.py`` holds the cold-interpreter proof.
    """
    if name in _PATHS_EXPORTS:
        from inbox_zero_connector.utils import paths

        return getattr(paths, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
