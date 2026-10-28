"""A folder the operator NAMED and the mailbox does not have must be said out loud.

THE VARIANT NEITHER STREAK CAN SEE (#191, third refutation). Both silence
alarms in ``loop._SilenceStreaks`` are inferences from what a round OBSERVED --
one counts rounds that polled no stream, the other counts rounds where mail
arrived and the scope matched none of it. On Microsoft Graph a misspelled
``only_listed`` folder is invisible to both at once:

- ``no_stream`` cannot fire, because the CORRECTLY spelled folders beside it
  still yield streams and keep ``streams_attempted`` above zero.
- ``out_of_scope`` cannot fire, because Graph fans out to one stream per
  SELECTED folder. The misspelled one is never selected, so its mail is never
  fetched; the round has nothing to be out of scope ABOUT, and
  ``skipped_out_of_scope`` stays at zero.

So the invoices pile up in a folder nothing polls, every count the loop keeps
reads healthy, and the daemon logs INFO forever -- verbatim the issue's
"observable in the log, never alarming", on the shape a typo actually takes:
ONE wrong name beside several right ones. An operator who misspells every
folder notices on day one because nothing is ever filed.

``_warn_unmatched_scope_entries`` asks a different question, and that is why it
sees this. It does not wait for a round to observe anything: it compares the
folders the operator NAMED against the folders the provider ENUMERATED, on the
first pass, on both providers. An entry matching no container is wrong from the
moment the config is loaded and stays wrong until a human edits it, so there is
no streak to count and none is counted.

MATCHING IS ``MailboxScope``'s OWN (``_named_by``), reached deliberately across
the private boundary. ``scope.py`` documents at length why ``covers`` is the
single scope question -- a second implementation of folder matching is a second
chance to disagree about which folder a client named -- and a private COPY of
that normalisation in ``loop.py`` would be exactly that second implementation,
free to rot. These tests pin the reuse: a nested Gmail child satisfies its
listed parent here for the same reason it does in ``covers``.

A FOURTH REFUTATION IS PINNED HERE, AND IT IS THIS SIGNAL'S OWN DEFECT. The
guard asked a PROXY -- does any enumerated container NAME match this entry --
while stream selection asks ``scope.covers``. Those disagree, because
``scope.py`` decides the well-known Junk/Deleted/Archive folders BEFORE the
mode: ``only_listed=("Inbox", "Archive")`` with the shipped default
``include_archive=False`` matched ``Archive`` by name, suppressed its own
warning, and was refused a stream anyway. Measured on Graph: 60 rounds, the
folder never polled, nothing filed, zero warnings of any kind -- byte-identical
to the harm this signal exists for, and reachable straight from connector config
because no validator refuses the combination.

The guard now asks the ANSWER: an entry is satisfied only when some enumerated
container both is NAMED by it and is one ``_stream_ids`` SELECTS, through the
same public ``covers`` call, so the report and the selection cannot drift. The
two causes then get two sentences, because they need two different edits -- a
misspelling wants the name corrected, a refused system folder wants
``include_archive``/``include_junk``/``include_deleted`` turned on, and sending
the second operator to check a spelling that is already right tells them nothing
true.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from loguru import logger

from _watch_harness import FakeProvider, _context
from inbox_zero_connector.providers.base import ChangeSet, Container
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch.scope import MailboxScope

TYPO = "Invoces"
"""``Invoices`` with one character missing. The whole defect, spelled out."""


def _container(container_id: str, name: str, *, well_known: str | None = None) -> Container:
    """One enumerated folder, in the shape both providers report."""
    return Container(
        id=container_id,
        name=name,
        well_known=well_known,
        parent_id=None,
        exclusive=well_known is not None,
    )


INBOX = _container("inbox-id", "Inbox", well_known="inbox")
INVOICES = _container("invoices-id", "Invoices")
ARCHIVE = _container("archive-id", "Archive", well_known="archive")
JUNK = _container("junk-id", "Junk Email", well_known="junk")
DELETED = _container("deleted-id", "Deleted Items", well_known="deleted")


@pytest.fixture(autouse=True)
def _rearmed_entry_latch() -> Iterator[None]:
    """Clear the once-per-process latch around every test.

    ``loop._unmatched_entries_warned`` is process-scoped on purpose: a
    60-second daemon must say a true thing once rather than 1,440 times a day.
    Inside one pytest process that would make whichever test runs SECOND on a
    given folder name silent, so the suite's result would depend on test ORDER.
    """
    watchloop._unmatched_entries_warned.clear()
    yield
    watchloop._unmatched_entries_warned.clear()


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Every WARNING-or-above line loguru emitted, as plain strings.

    A loguru SINK rather than ``caplog``: this package logs through loguru,
    which does not propagate into the stdlib ``logging`` tree, so a ``caplog``
    assertion here would run against an always-empty list.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{message}")
    yield lines
    logger.remove(sink_id)


class _GraphMailbox(FakeProvider):
    """A Graph-shaped mailbox: one stream per SELECTED folder, exclusive containers.

    ``name = "microsoft"`` is load-bearing -- it is what makes ``_stream_ids``
    filter by scope instead of returning Gmail's single mailbox-wide stream, so
    a folder the scope does not select is genuinely never polled here.
    """

    name = "microsoft"

    def __init__(self, containers: tuple[Container, ...] = (INBOX, INVOICES)) -> None:
        super().__init__(ChangeSet(cursor="delta-1"), containers=containers)


class _GmailMailbox(FakeProvider):
    """The kit's DEFAULT provider: one mailbox-wide stream whatever the scope says."""

    name = "google"

    def __init__(self, containers: tuple[Container, ...] = (INBOX, INVOICES)) -> None:
        super().__init__(ChangeSet(cursor="history-1"), containers=containers)


def _scope(*folders: str, nesting: bool = False) -> MailboxScope:
    """An ``only_listed`` scope naming exactly ``folders``."""
    return MailboxScope(mode="only_listed", included_folders=folders, names_encode_nesting=nesting)


def _unmatched(warnings: list[str]) -> list[str]:
    """The MISSPELLING lines only, so a streak line cannot satisfy a count."""
    return [line for line in warnings if "has no folder matching it" in line]


def _refused(warnings: list[str]) -> list[str]:
    """The lines naming an entry the mailbox HAS and the scope still refuses.

    A separate helper rather than a widened ``_unmatched``, because the two
    causes need different edits and a test that cannot tell them apart is how
    the wrong sentence shipped: an operator sent to check a spelling that is
    already correct has been told nothing true.
    """
    return [line for line in warnings if "the watcher is refused it anyway" in line]


def _entry_lines(warnings: list[str]) -> list[str]:
    """Every per-folder entry WARN, whichever cause it names.

    The union of the two cause helpers rather than a match on the shared
    prefix. The streak line's own text refers to these lines, so a prefix match
    would count a streak as an entry line -- which is how a test asserting
    "one entry line" silently passed on one entry line plus one streak.
    """
    return _unmatched(warnings) + _refused(warnings)


def test_a_graph_folder_that_yields_no_stream_is_named_on_the_first_pass(
    warnings: list[str],
) -> None:
    """VARIANT W: the misspelled folder is never polled, and the operator is told anyway.

    RED before the fix: 50 rounds, ``streams_polled=1`` every round because the
    correctly-spelled Inbox kept working, ``skipped_out_of_scope=0`` because the
    invoices were never fetched, and ZERO warnings. Here one pass is enough --
    the entry matched no container, which needs no mail to be true.
    """
    provider = _GraphMailbox()
    context = _context(provider, scope=_scope("Inbox", TYPO))

    result = watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert result is not None
    assert provider.polled_containers == ["inbox-id"], (
        "the misspelled folder must still not be polled -- reporting is not widening"
    )
    lines = _unmatched(warnings)
    assert len(lines) == 1, warnings
    assert f"folder='{TYPO}'" in lines[0]


def test_the_line_names_the_closest_spellings_the_mailbox_actually_has(
    warnings: list[str],
) -> None:
    """An actionable line names the folder the operator probably meant.

    "No folder matches" is true and useless at 3am. The mailbox has ``Invoices``
    and the config says ``Invoces``; naming the near miss turns the line into
    the edit.
    """
    context = _context(_GraphMailbox(), scope=_scope("Inbox", TYPO))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    warned = _unmatched(warnings)[0]
    assert "closest folders this mailbox has: 'Invoices'" in warned
    assert "Fix:" in warned


def test_the_gmail_equivalent_is_named_too(warnings: list[str]) -> None:
    """The same config on the kit's DEFAULT provider gets the same line.

    Gmail applies scope per MESSAGE inside the round rather than at stream
    selection, so the streak CAN eventually see this shape -- but only after
    three rounds and only if invoices actually arrive in the meantime. The
    config fact is true before any mail does, and is reported the same way on
    both providers so an operator does not learn two different failure modes.
    """
    provider = _GmailMailbox()
    context = _context(provider, scope=_scope("Inbox", TYPO))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert provider.polled_containers == [None], "Gmail still polls its one stream"
    lines = _unmatched(warnings)
    assert len(lines) == 1, warnings
    assert f"folder='{TYPO}'" in lines[0]


def test_every_unmatched_entry_gets_its_own_line(warnings: list[str]) -> None:
    """Three bad names produce three lines, not one line about the first.

    The latch is keyed by ENTRY rather than by scope for exactly this: an
    operator who fixes the one folder they were told about and restarts must not
    then be told about the next one, and the next.
    """
    context = _context(_GraphMailbox(), scope=_scope("Inbox", TYPO, "Recietps", "Contarcts"))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    lines = _unmatched(warnings)
    assert len(lines) == 3, warnings
    named = {TYPO, "Recietps", "Contarcts"}
    assert {entry for entry in named if any(f"folder='{entry}'" in line for line in lines)} == named


def test_the_line_is_emitted_once_per_process_not_once_per_round(
    warnings: list[str],
) -> None:
    """A 60-second daemon says a true thing once, not 1,440 times a day.

    The negative bound on the signal. Nothing about the config changes between
    rounds, so repeating it every round would be noise an operator filters --
    which costs exactly as much as the silence this replaced.
    """
    control = watchloop.LoopControl(install_handler=None)
    context = _context(_GraphMailbox(), scope=_scope("Inbox", TYPO))

    class _StoppingSleep:
        """Stop the loop after a fixed number of inter-pass waits."""

        def __init__(self) -> None:
            self.waits = 0

        def __call__(self, _delay_seconds: float) -> None:
            self.waits += 1
            if self.waits >= 40:
                control.request_stop()

    watchloop.run(context, sleep=_StoppingSleep(), control=control)

    assert len(_unmatched(warnings)) == 1, warnings


def test_a_scope_every_entry_of_which_matches_is_never_accused(warnings: list[str]) -> None:
    """The negative control: a CORRECT ``only_listed`` scope says nothing.

    A false positive here would be worse than the silence, because it would
    train an operator to ignore the line on the installs that are fine.
    """
    context = _context(_GraphMailbox(), scope=_scope("Inbox", "Invoices"))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert _unmatched(warnings) == []


@pytest.mark.parametrize("mode", ["inbox_only", "all_folders"])
def test_a_mode_with_no_inclusion_list_is_never_accused(warnings: list[str], mode: str) -> None:
    """Only ``only_listed`` names folders, so only it can name one that is missing.

    ``all_except`` is deliberately excluded too: an exclusion naming a folder
    the mailbox does not have excludes nothing, which is harmless -- the
    connector reads LESS than the operator asked for in the ``only_listed``
    case and exactly what they asked for here.
    """
    context = _context(_GraphMailbox(), scope=MailboxScope(mode=mode))  # type: ignore[arg-type]

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert _unmatched(warnings) == []


def test_an_all_except_entry_that_matches_nothing_is_never_accused(
    warnings: list[str],
) -> None:
    """An exclusion that matches no folder excludes nothing, which is not a defect.

    Stated as its own test because the asymmetry is the point: a MISSING
    inclusion silently narrows what the connector reads to less than the client
    agreed to watch, while a missing exclusion leaves the answer exactly as the
    client gave it.
    """
    context = _context(
        _GraphMailbox(),
        scope=MailboxScope(mode="all_except", excluded_folders=("Recietps",)),
    )

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert _unmatched(warnings) == []


def test_matching_is_case_and_whitespace_insensitive_like_covers(
    warnings: list[str],
) -> None:
    """A config that differs only in case or padding named the folder correctly.

    ``covers`` casefolds and strips as a SECURITY property, not a convenience --
    ``scope.py`` records that a case-sensitive comparison would read a folder a
    client excluded. This signal reuses that same normalisation, so it must not
    accuse an operator whose only sin is capitalising ``INBOX``.
    """
    context = _context(_GraphMailbox(), scope=_scope("  INBOX  ", "invoices"))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert _unmatched(warnings) == []


def test_a_listed_gmail_parent_is_matched_by_its_nested_child(warnings: list[str]) -> None:
    """Reusing ``MailboxScope``'s matcher means nesting is honoured for free.

    On a Gmail install (``names_encode_nesting=True``) a label named
    ``Clients/Smith`` is the CHILD of a listed ``Clients``, and ``covers``
    admits it. A private copy of the matching in ``loop.py`` would have to
    reimplement the boundary rule that keeps ``MyClients`` out of it, and a
    copy that got it wrong would accuse an operator whose scope works.
    """
    provider = _GmailMailbox(containers=(INBOX, _container("child-id", "Clients/Smith")))
    context = _context(provider, scope=_scope("Inbox", "Clients", nesting=True))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert _unmatched(warnings) == []


def test_a_graph_install_does_not_read_a_slash_as_nesting(warnings: list[str]) -> None:
    """The other direction of the same reuse, and the reason it must be reused.

    ``names_encode_nesting=False`` is the Graph answer: a folder literally named
    ``Clients/Smith`` is ONE folder whose name contains a slash, and it is NOT a
    child of anything. So a scope naming ``Clients`` matches nothing here and is
    correctly reported -- the opposite verdict on the same two strings, decided
    by the installed field rather than by this module.
    """
    provider = _GraphMailbox(containers=(INBOX, _container("child-id", "Clients/Smith")))
    context = _context(provider, scope=_scope("Inbox", "Clients"))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    lines = _unmatched(warnings)
    assert len(lines) == 1, warnings
    assert "folder='Clients'" in lines[0]


def test_a_listing_failure_accuses_nobody(warnings: list[str]) -> None:
    """A pass that never listed has no folders to compare against, so it says nothing.

    The same rule the streaks apply one latch over: an absent observation is not
    evidence. Accusing the config here would blame the operator for a WAN blip,
    on exactly the flaky mailbox where the real defect is hardest to see.
    """

    class _FailingListing(_GraphMailbox):
        """A Graph mailbox whose container listing never succeeds."""

        def list_containers(self) -> list[Container]:
            raise RuntimeError("listing is down")

    context = _context(_FailingListing(), scope=_scope("Inbox", TYPO))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert _unmatched(warnings) == []


def test_an_empty_listing_says_the_mailbox_lists_no_folders(warnings: list[str]) -> None:
    """A successful listing that enumerated nothing is a real, reportable state.

    Distinct from the failure above: the provider ANSWERED, and the answer was
    that this account can see no folders at all. ``difflib`` has nothing to rank,
    so the line says so rather than printing an empty suggestion list.

    ``list_containers`` is OVERRIDDEN rather than constructed with an empty
    tuple, because ``FakeProvider.__init__`` reads ``containers or <defaults>``
    -- an empty tuple is falsy there and silently becomes the harness's two
    default folders, which would make this test assert the opposite of its name.
    """

    class _EmptyListing(_GraphMailbox):
        """A Graph mailbox that successfully enumerates nothing."""

        def list_containers(self) -> list[Container]:
            return []

    context = _context(_EmptyListing(), scope=_scope("Inbox", TYPO))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    lines = _unmatched(warnings)
    assert len(lines) == 2, warnings
    assert all("no folders at all" in line for line in lines)


def test_a_name_with_no_near_miss_lists_what_the_mailbox_does_have(
    warnings: list[str],
) -> None:
    """When nothing is close, the line offers the real names instead of a bad guess.

    ``difflib``'s cutoff exists so a suggestion is a suggestion. A folder called
    ``Zzzzzz`` resembles nothing here, and inventing a nearest match would send
    the operator to rename a folder that was never the problem.
    """
    context = _context(_GraphMailbox(), scope=_scope("Inbox", "Zzzzzz"))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    warned = _unmatched(warnings)[0]
    assert "nothing in this mailbox is close to it" in warned
    assert "'Inbox'" in warned


@pytest.mark.parametrize(
    ("folder", "container", "setting"),
    [
        ("Archive", ARCHIVE, "include_archive"),
        ("Junk Email", JUNK, "include_junk"),
        ("Deleted Items", DELETED, "include_deleted"),
    ],
)
def test_a_named_well_known_folder_that_yields_no_stream_is_named_too(
    warnings: list[str], folder: str, container: Container, setting: str
) -> None:
    """The entry produced NO STREAM, so the operator is told -- name match or not.

    RED before the fix: 60 rounds, ``archive-id`` never polled, nothing filed,
    and ZERO warnings of any kind. The guard asked a PROXY -- does any container
    NAME match this entry -- while stream selection asks ``scope.covers``, and
    ``scope.py`` decides the well-known Junk/Deleted/Archive folders BEFORE the
    mode. So ``only_listed=("Inbox", "Archive")`` with the shipped default
    ``include_archive=False`` matched by name, suppressed its own warning, and
    was refused a stream anyway. That is byte-identical to the harm #191 exists
    for -- one bad entry beside a working Inbox, mail piling up in a folder
    nothing polls -- and it is reachable straight from connector config, because
    no validator refuses the combination.

    The guard now asks the ANSWER: an entry is satisfied only when some
    enumerated container both is named by it AND is one ``_stream_ids`` selects,
    through the same public ``covers`` call.
    """
    provider = _GraphMailbox(containers=(INBOX, container))
    context = _context(provider, scope=_scope("Inbox", folder))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert provider.polled_containers == ["inbox-id"], (
        "precondition: the named folder is genuinely never polled"
    )
    lines = _refused(warnings)
    assert len(lines) == 1, warnings
    assert f"folder='{folder}'" in lines[0]
    assert setting in lines[0]


def test_the_refused_well_known_line_does_not_blame_a_spelling(
    warnings: list[str],
) -> None:
    """The mailbox HAS the folder, so the line must not send anyone to check it.

    The second half of the same defect, and the reason the two causes get two
    sentences rather than one shared one. The misspelling text -- "this mailbox
    has no folder matching it ... correct the name" -- is confidently wrong here:
    the operator spelled ``Archive`` exactly right, the refusal is
    ``include_archive``, and checking the spelling teaches them nothing. A wrong
    sentence in the grammar of a fact costs more than silence, because it removes
    the signal that anything still needs checking.
    """
    context = _context(_GraphMailbox(containers=(INBOX, ARCHIVE)), scope=_scope("Inbox", "Archive"))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    warned = _refused(warnings)[0]
    assert _unmatched(warnings) == [], "the misspelling text must not be reused here"
    assert "The spelling is not the problem." in warned
    assert "closest folders this mailbox has" not in warned
    assert "Fix: set include_archive to true" in warned
    assert "drop the folder from included_folders" in warned


def test_the_gmail_equivalent_of_the_refused_folder_is_named_too(
    warnings: list[str],
) -> None:
    """The same config on the kit's DEFAULT provider gets the same line.

    Gmail applies scope per MESSAGE rather than at stream selection, so its
    symptom differs -- the mail IS fetched and then discarded -- but the config
    fault is identical and must not read as two different failure modes. The
    streak can eventually see this shape on Gmail; it takes three rounds and
    mail actually arriving, and it cannot say WHICH entry is at fault.
    """
    provider = _GmailMailbox(containers=(INBOX, ARCHIVE))
    context = _context(provider, scope=_scope("Inbox", "Archive"))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert provider.polled_containers == [None], "Gmail still polls its one stream"
    lines = _refused(warnings)
    assert len(lines) == 1, warnings
    assert "folder='Archive'" in lines[0]


def test_a_well_known_folder_the_scope_admits_is_never_accused(warnings: list[str]) -> None:
    """The negative control: turning the include_ setting on silences the line.

    A false positive here would be worse than the original silence, because it
    would train an operator to ignore the line on the installs that are correct
    -- and this is the exact config the WARN tells them to write.
    """
    provider = _GraphMailbox(containers=(INBOX, ARCHIVE))
    scope = MailboxScope(
        mode="only_listed", included_folders=("Inbox", "Archive"), include_archive=True
    )
    context = _context(provider, scope=scope)

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert provider.polled_containers == ["inbox-id", "archive-id"], (
        "precondition: the folder is genuinely polled once its setting allows it"
    )
    assert _entry_lines(warnings) == []


def test_an_entry_naming_a_refused_folder_and_a_selected_one_is_never_accused(
    warnings: list[str],
) -> None:
    """One nested entry that yields SOME stream is satisfied, not accused.

    The bound on asking the answer instead of the name. A Gmail entry naming
    ``Clients`` covers a whole subtree, and a subtree can hold a folder the
    scope refuses beside folders it selects. The entry is doing its job in that
    case -- mail IS being watched under it -- so warning would be a false
    positive on a working config. The guard is therefore ANY selected container,
    not EVERY named one.
    """
    provider = _GmailMailbox(
        containers=(
            INBOX,
            _container("child-id", "Clients/Smith"),
            _container("clients-archive-id", "Clients/Archive", well_known="archive"),
        )
    )
    context = _context(provider, scope=_scope("Inbox", "Clients", nesting=True))

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert _entry_lines(warnings) == []


def test_each_cause_gets_its_own_line_when_both_are_configured(warnings: list[str]) -> None:
    """A misspelled entry and a refused system folder produce two distinct lines.

    The latch is keyed by ENTRY, so a config with both faults reports both --
    and each reports its OWN cause. An operator who fixes the spelling and
    restarts must then be told about the ``include_archive`` setting, not handed
    one line covering whichever fault the loop hit first.
    """
    context = _context(
        _GraphMailbox(containers=(INBOX, INVOICES, ARCHIVE)),
        scope=_scope("Inbox", TYPO, "Archive"),
    )

    watchloop.run(context, once=True, control=watchloop.LoopControl(install_handler=None))

    assert len(_entry_lines(warnings)) == 2, warnings
    assert f"folder='{TYPO}'" in _unmatched(warnings)[0]
    assert "folder='Archive'" in _refused(warnings)[0]
