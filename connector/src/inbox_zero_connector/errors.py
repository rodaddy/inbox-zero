"""The operator-refusal base. The one exception kind the CLI prints, not dumps.

WHAT THIS DISTINGUISHES
    Two things can go wrong in this connector and they want opposite
    treatment. A DEFECT -- an ``AttributeError`` off a None, a transport error
    nobody anticipated, a provider returning a shape the adapter did not expect
    -- is information, and its traceback is the whole of that information. It
    propagates, it prints its stack, and swallowing it turns a bug report into
    a shrug.

    A REFUSAL is the other thing. The install has no ``install_id``; the
    credential was never minted; a config layer names a folder that does not
    exist. Nothing is broken -- the connector is working exactly as designed
    and is declining to proceed -- and the message is already a complete
    instruction to the person at the terminal, written to be read. Putting a
    stack over the top of that sentence does not add information; it buries
    the only line that mattered.

    ``OperatorRefusalError`` is the type that says which one a given exception is.
    Nothing catches it to swallow it: the CLI's top-level handler RENDERS it as
    one stderr line and returns a deliberate ``1``, and the five-point logging
    wrapper still records the failure at ERROR with its ``error_type`` and
    still re-raises. Only the traceback is withheld, and only for this base.

WHY A BASE CLASS AND NOT A TUPLE
    The behaviour lives at three seams -- ``apps/cli/main.py``'s handler,
    ``utils/logging_config.py``'s failure point, and every future one -- and a
    tuple of concrete types has to be found and edited at each of them when a
    sibling refusal is added. That edit is the one that gets forgotten, and the
    forgetting is silent: the new refusal simply prints a traceback and nobody
    notices until a smoke test on a fresh install does. #171 shipped the
    ``ConfigError`` half and #195 found ``AuthError`` had none of it for
    exactly this reason -- the treatment was keyed on a concrete class.

    Inheriting instead makes the question ``isinstance(exc, OperatorRefusalError)``,
    which a new refusal answers correctly the moment it is declared, at its own
    definition site, where the author is already thinking about which kind of
    thing they are writing.

WHY IT LIVES IN ITS OWN MODULE
    ``config.ConfigError`` and ``providers.base.AuthError`` are in different
    trees and must stay there: ``AuthError`` remains a ``ProviderError``,
    because the provider layer's own callers catch that and would start missing
    it, and ``ConfigError`` stays the thing the watch loop treats as fatal.
    Neither package may import the other -- ``config`` is the keystone and
    ``providers`` depends on it -- so a base that both inherit cannot live in
    either. It lives here, at the package root, importing nothing, so both can
    reach it without an edge that does not already exist.

    Importing nothing is load-bearing beyond tidiness. ``utils.logging_config``
    defers its ``config`` import to break a cycle (``config`` imports the
    logger at its own top level); a base with no imports of its own is safe to
    import at module level from anywhere, including from inside that cycle.
"""

from __future__ import annotations


class OperatorRefusalError(Exception):
    """The install is not set up to do this. Not a defect, and not a stack.

    Subclass this when the message is a complete instruction to the person who
    installed the connector and the correct response is for them to run a
    command -- not for anyone to read a traceback. Do NOT subclass it for a
    fault a retry could plausibly clear, or for anything whose cause is inside
    this package: those keep their traceback, which is what makes them
    diagnosable.
    """
