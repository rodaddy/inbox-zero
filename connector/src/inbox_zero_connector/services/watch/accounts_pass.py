"""ONE round across EVERY configured account. The multi-account fan-out (#69).

WHAT THIS MODULE IS
    The layer between ``loop.run``, which owns the clock and the health file,
    and ``loop.run_pass``, which owns one mailbox's streams. A round is now
    "poll each configured account once", and this is the only place that
    sequence lives. Each account brings its OWN ``RoundContext`` -- its own
    provider built from its own credential, its own scope, its own drafts
    answer -- and the taxonomy inside those contexts is the SAME object,
    because decision D11 makes the folder vocabulary shared across an install
    and posture plus drafts per account.

WHY A SEPARATE MODULE AND NOT MORE OF ``loop.py``
    ``loop.py`` sits inside the code-line ceiling
    (``scripts/check_code_size.py``) with the streak and health machinery
    already in it, and the fan-out is a genuinely different concern from the
    clock: this decides WHICH mailboxes a round covers, the loop decides WHEN
    the next round happens and what the watcher's health is. Splitting them
    means the account iteration can be tested with no clock and no health file
    at all, which is what the two-account test does.

ONE COMBINED RESULT, AND THAT IS WHAT MAKES THE REST UNCHANGED
    ``run_accounts`` returns a single ``PassResult`` whose ``streams`` are
    every account's streams concatenated. The silence streaks, the health
    reporter, the backoff, and the ``--once`` exit contract all read that one
    object exactly as they did when a round was one mailbox, so none of them
    needed a multi-account arm. A two-account round where one account errors
    therefore backs off and reports failing -- the WORST account's state is the
    round's state, which is the honest reading for a health file that describes
    one process.

    THE COST OF THAT CHOICE, STATED: a per-account failure is not individually
    addressable in the health file. One dead credential on the second mailbox
    marks the whole watcher failing while the first mailbox keeps filing
    normally. That is deliberate for now -- the alternative is a health
    document per account, which is a schema change to a file another lane owns
    -- and the per-account log lines below are where an operator sees which
    mailbox is the failing one.

WHAT IT DELIBERATELY DOES NOT OWN
    It builds no provider, reads no config, and makes no scope or posture
    decision. Every one of those is made once at the CLI boundary
    (``apps/cli/main.py``) where the settings object lives, and arrives here
    already resolved into a context per account. A module that reached back for
    settings would be a second place drafts get turned on, which is the exact
    shape of widening this package refuses everywhere else.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from inbox_zero_connector.services.watch.round import RoundContext

type PassCallable[T] = Callable[[RoundContext, Callable[[], object] | None], T]
"""``loop.run_pass``'s shape, parameterised over what a pass returns.

GENERIC RATHER THAN TYPED TO ``PassResult`` because naming that type here would
mean importing ``loop``, which imports this module -- a cycle at import time.
The type variable keeps the caller's own result type all the way through, so
``loop`` gets a ``list[PassResult]`` back and needs no cast or isinstance
filter to prove it.
"""


@dataclass(frozen=True)
class AccountRound:
    """One configured mailbox and the fully-resolved context that polls it.

    A PAIR RATHER THAN A FIELD ON ``RoundContext`` because the address is not
    an input to a polling round -- ``RoundContext.account`` carries it for the
    receipt, and this dataclass carries it for the LOG and for the caller that
    assembled the list. Keeping the assembly's own record beside the context
    means this module never has to read a field off the context to name what it
    is running, and a caller that forgot to stamp ``account`` gets a log line
    that still says which mailbox it meant.
    """

    address: str
    """The mailbox address, as configured. Prefixes this account's round line."""

    context: RoundContext
    """This account's own provider, scope, and state. Taxonomy is SHARED (D11)."""


def run_accounts[T](
    rounds: Sequence[AccountRound],
    run_pass: PassCallable[T],
    log_round: Callable[[T, str], None],
    checkpoint: Callable[[], object] | None = None,
) -> list[T]:
    """Poll every account once, logging each account's own round line.

    THE TWO CALLABLES ARE INJECTED RATHER THAN IMPORTED, and the reason is a
    cycle: ``loop`` imports this module to run a round, so this module importing
    ``loop`` back for ``run_pass`` and ``_log_round`` would close the loop at
    import time. Passing them in also makes the two-account test drive this
    function with two recording stubs and no provider at all.

    NO ACCOUNT'S FAILURE SKIPS THE NEXT ONE. ``run_pass`` already isolates a
    stream's exception into an errored ``StreamResult``, and the only thing it
    re-raises is ``OperatorRefusalError`` -- a configuration fault that is
    identical for every account on the install and is meant to reach the CLI.
    So nothing is caught here: a per-account ``try`` would swallow exactly the
    refusal the CLI's one-line handler exists for.

    Args:
        rounds: The configured accounts, in configured order. The first is the
            install's default account and is polled first, which is what keeps
            a single-account install's log and ledger ordering unchanged.
        run_pass: ``loop.run_pass``, injected.
        log_round: ``loop._log_round``, injected.
        checkpoint: Passed through to each account's pass unchanged.

    Returns:
        One pass result per account, in the order they were polled.
    """
    results: list[T] = []
    for entry in rounds:
        result = run_pass(entry.context, checkpoint)
        # PER ACCOUNT, BEFORE THE COMBINED LINE. An operator reading a
        # multi-account log needs the mailbox name on the counts, because
        # "filed 3, failed 1" is unactionable when two mailboxes produced it.
        log_round(result, entry.address)
        results.append(result)
    return results
