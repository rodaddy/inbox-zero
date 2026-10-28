"""The shared well-known vocabulary both adapters answer ``list_containers`` in.

Split out of the two adapters for the reason ``gmail_ids`` records: both sat at
the 500-code-line ceiling, and the alternative to moving whole declarations out
is COMPRESSING working code, which has already shipped a real regression in this
package once. Moving a declaration costs nothing -- the same values are read by
the same callers -- and the budget comes from the file boundary.

It also earns its own module on the merits. ``Container.well_known`` promises
that "inbox" from Graph and "inbox" from Gmail are THE SAME TAG, and that promise
is only checkable if the two tables are readable side by side. Held one per
adapter, the two vocabularies could drift by a single string and nothing would
say so until a caller silently matched neither.

WHAT THE ASYMMETRY BETWEEN THE TWO TABLES MEANS
    Graph has five entries and Gmail four. Gmail has no ``archive``, and that is
    Google's model rather than an omission here: archiving a Gmail message IS
    the removal of INBOX, so the state is the ABSENCE of a container and there
    is no row to report. A caller that needs "is this archived" must ask that
    question differently on Gmail, which is exactly the kind of difference this
    model exists to surface instead of hiding behind a fabricated tag.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, NamedTuple

from loguru import logger

from inbox_zero_connector.providers.base import Container, ProviderError

INBOX: Final = "inbox"
JUNK: Final = "junk"
DELETED: Final = "deleted"
ARCHIVE: Final = "archive"
SENT: Final = "sent"

WELL_KNOWN_TAGS: Final[frozenset[str]] = frozenset({INBOX, JUNK, DELETED, ARCHIVE, SENT})
"""Every tag ``Container.well_known`` may carry. Anything else is ``None``.

Named constants rather than bare strings in the tables below so a typo is an
``AttributeError`` at import instead of a tag that silently matches nothing.
"""

GRAPH_WELL_KNOWN: Final[dict[str, str]] = {
    "inbox": INBOX,
    "junkemail": JUNK,
    "deleteditems": DELETED,
    "archive": ARCHIVE,
    "sentitems": SENT,
}
"""Graph well-known folder ALIASES -> shared tag.

The keys are the names Graph accepts in a URL (``/me/mailFolders/junkemail``),
not display names and not ids. The v1.0 ``mailFolder`` resource carries no
``wellKnownName`` property at all, so an adapter learns which real folder is the
junk folder by GETting the alias and keeping the id that comes back. Matching a
DISPLAY NAME instead would be wrong twice over: the name is localised, and a
user may create a folder called "Junk" that is not the junk folder.
"""

GMAIL_WELL_KNOWN: Final[dict[str, str]] = {
    "INBOX": INBOX,
    "SPAM": JUNK,
    "TRASH": DELETED,
    "SENT": SENT,
}
"""Gmail SYSTEM label IDS -> shared tag.

Keyed on the id, which Gmail fixes for system labels and never localises. The
same row's ``name`` IS translated per user, so matching on it would break on any
non-English mailbox -- the same class of defect the Graph note above describes.

No ``archive`` key; see the module docstring.
"""


class GmailLabelRow(NamedTuple):
    """The ``id`` and ``name`` of a Gmail ``Label`` row that PARSED.

    A value of this type is the proof, not a hint: ``parse_gmail_label_row`` is
    the only thing that constructs one, and it constructs one only after both
    fields have been checked, so every field is already a usable ``str``. That
    is what lets both readers below stop re-deciding "is this row good" for
    themselves -- the question is answered once, by returning this or ``None``.
    """

    id: str
    name: str


def usable_label_id(value: Any) -> str | None:
    """A Gmail label ``id`` that can be USED, or None -- the one id test (#202).

    Used means: a ``str``, and not blank. Everything else is a malformed row
    and there is no repair that does not invent a value.

    A BLANK ID IS MISSING, NOT A VALUE. ``""`` once passed an ``is None`` test
    and reached ``users.messages.modify`` as
    ``{'addLabelIds': [''], 'removeLabelIds': ['INBOX']}`` -- a request that
    ARCHIVES the message (removing INBOX is how Gmail archives) while adding a
    label that does not exist. Whitespace-only is the same answer for the same
    reason.

    AN ID THAT IS NOT A ``str`` IS MISSING TOO, AND IS NEVER STRINGIFIED. The
    Gmail Label resource types ``id`` as a string; ``["Label_9"]``, ``7``,
    ``False``, and ``None`` are malformed responses, and ``str()`` on any of
    them mints a plausible-looking id (``"['Label_9']"``, ``"7"``, ``"False"``,
    ``"None"``) that cannot exist in the mailbox.

    Returning the value rather than a bool is what makes this the SOURCE of the
    id both readers use, instead of a predicate they could pass and then read
    the field again their own way -- which is exactly how the two readers came
    to disagree (#202).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def parse_gmail_label_row(raw: Any) -> GmailLabelRow | None:
    """One ``users.labels.list`` row, parsed, or None when it is not usable (#202).

    THE ONE PLACE A LABEL ROW IS JUDGED. ``gmail_labels_to_containers`` and
    ``find_label_id`` read the SAME listing, and #186 hardened them one at a
    time: the lookup refused a row through ``label_id_or_refuse`` while the
    container reader still called ``str(raw.get("id", ""))`` on it. The two
    readers therefore disagreed about the same row -- one skipped it, the other
    minted a ``Container`` whose id was ``"['Label_9']"``, ``"7"``, ``"None"``,
    or ``""`` -- and a fabricated container id is a filing destination and a
    routing-index key. Two blank-id rows even COLLIDED in
    ``services/watch/round.py:_container_index``, so one real folder vanished
    from the index and ``_covered`` then refused mail in it as out of scope.

    Usable means: the row is a mapping, its ``id`` passes ``usable_label_id``,
    and its ``name`` is a ``str``. A blank NAME is legal where a blank id is
    not: Gmail does not mint one, but a name is only ever displayed and matched,
    while an id is written to the mailbox.

    None rather than a raise, because a row we cannot read cannot be a container
    and the healthy rows beside it are still true. ``list_containers`` runs on
    every watch round and on the reset path, so refusing a whole mailbox's
    routing table over one unreadable row is strictly the worse answer. The
    CALLER decides what the skip means -- ``gmail_labels_to_containers`` counts
    it at WARNING, ``find_label_id`` reads it as "not the match" -- which is the
    one judgement that legitimately differs between them.

    Silent here, deliberately: this runs once per row and the two callers own
    the reporting between them, so logging inside would double every line the
    container reader already emits with a count.
    """
    if not isinstance(raw, dict):
        return None
    label_id = usable_label_id(raw.get("id"))
    name = raw.get("name")
    if label_id is None or not isinstance(name, str):
        return None
    return GmailLabelRow(id=label_id, name=name)


def gmail_label_to_container(raw: Any) -> Container | None:
    """One Gmail ``Label`` row as a ``Container``, or None when the row is unusable.

    The judgement is ``parse_gmail_label_row``'s (#202); this function is only
    the ``Container`` shape around a row that already parsed, which is why it
    has no checks of its own left to drift from the lookup's.

    ``parent_id`` is ALWAYS None, and that is a statement about Gmail rather
    than a shortcut. The Label resource carries no parent field at all: a label
    named "Clients/Acme" renders nested in the UI, but the "/" is a display
    convention the API never promises, so splitting on it would be inventing
    hierarchy the provider never stated -- and would misparse a user's label
    that merely contains a slash.

    ``exclusive=False`` because a Gmail message routinely carries INBOX,
    IMPORTANT, and a user label at the same time.
    """
    row = parse_gmail_label_row(raw)
    if row is None:
        return None
    return Container(
        id=row.id,
        name=row.name,
        well_known=GMAIL_WELL_KNOWN.get(row.id),
        parent_id=None,
        exclusive=False,
    )


def graph_folder_to_container(
    folder_id: str, display_name: str, parent_id: str | None, well_known: dict[str, str]
) -> Container:
    """One Graph mail folder as a ``Container``. Exclusive, always.

    ``well_known`` maps REAL FOLDER ID -> shared tag, resolved by the adapter
    from Graph's own aliases. It is passed in rather than looked up here because
    resolving it costs one HTTP request per alias, which is the adapter's job;
    this function stays pure so the mapping rule is testable without a
    transport.

    ``exclusive=True`` because a Graph message sits in exactly one folder.
    """
    return Container(
        id=folder_id,
        name=display_name,
        well_known=well_known.get(folder_id),
        parent_id=parent_id,
        exclusive=True,
    )


MAX_FOLDER_DEPTH: Final[int] = 6
"""How many levels of Graph child folders ``walk_graph_folders`` will visit.

``GET /me/mailFolders`` returns ROOT-LEVEL FOLDERS ONLY -- a child is reachable
only through its parent's ``childFolders`` collection -- so enumerating an
Outlook mailbox is a TRAVERSAL, and an unbounded traversal is a request
amplifier driven by mailbox structure the connector does not control.

Six because real mailboxes nest two or three levels and Outlook's own UI stops
being usable well before six; it leaves generous headroom while keeping the
worst case finite. Depth beyond it is reported by a container's absence, not by
a crash: the walk stops and returns what it proved. Gmail needs no equivalent --
``users.labels.list`` returns every label flat in one response.
"""

GraphFetch = Callable[..., dict[str, Any]]
"""The adapter's validated GET, taking path segments. Injected, not imported.

Passing the fetch in is what keeps this module transport-free and testable
without a mock transport, while leaving URL construction -- the security
boundary -- inside the adapter where ``_graph_path`` validates every segment.

It also accepts a ``link`` keyword, and that is NOT a hole in the boundary:
``link`` is a server-supplied ``@odata.nextLink`` replayed VERBATIM, never
assembled here and never parsed. Segments-with-validation stays the only way to
NAME a path; a continuation is Graph's own opaque state and the adapter's
``_get`` already carries the same rule for ``_folder_id``.
"""

MAX_FOLDER_PAGES: Final[int] = 50
"""How many pages of ONE folder listing ``walk_graph_folders`` will follow.

The same class of bound as ``MAX_FOLDER_DEPTH`` and for the same reason:
following ``@odata.nextLink`` until the server stops offering one is a loop
whose length the connector does not control, and a service that answered every
page with another link would spin forever. At ``MAX_PAGE_SIZE`` (200) rows per
page this admits 10,000 folders in one collection, which no real mailbox
approaches -- it is a stop, not a budget.

It is NOT ``cap_page``: that clamps how many rows we ASK for per page, which is
a different question from how many pages we will follow. Both bounds apply, and
``_container_fetch`` already supplies the page size.
"""


def resolve_graph_well_known(fetch: GraphFetch) -> dict[str, str]:
    """Map REAL Graph folder id -> shared tag, one GET per well-known alias.

    Resolved by ID because the v1.0 ``mailFolder`` resource has no
    ``wellKnownName`` property: GETting ``/me/mailFolders/junkemail`` is the
    only way to learn which real folder Graph considers the junk folder.

    A mailbox may legitimately lack an alias -- ``archive`` does not exist on
    every Exchange configuration -- so a failed resolution is SKIPPED rather
    than raised. The absence of a tag is a truthful answer here; nothing is
    being PROVEN about a message, which is what makes this different from the
    sent-folder resolution that refuses on absent evidence.
    """
    resolved: dict[str, str] = {}
    for alias, tag in GRAPH_WELL_KNOWN.items():
        try:
            folder = fetch("me", "mailFolders", alias, operation="resolve_well_known")
        except ProviderError:
            # DOCUMENTED FAIL-OPEN, argued in this function's docstring: a
            # mailbox may legitimately lack an alias (`archive` does not exist
            # on every Exchange configuration), so an unresolvable alias is a
            # truthful absence rather than an error. Nothing is being PROVEN
            # about a message here, which is what separates this from the
            # sent-folder resolution that refuses on absent evidence.
            #
            # DEBUG, not WARNING: on a mailbox without `archive` this fires on
            # every single resolution pass, and a warning that is normal
            # teaches operators to ignore warnings. The line still matters --
            # without it, a mailbox where EVERY alias fails (a scope regression
            # or a Graph outage) is indistinguishable from one that simply has
            # no optional folders, and both produce an empty tag map in silence.
            logger.opt(exception=True).debug(
                f"CONTAINERS: well-known alias unresolved alias={alias} tag={tag} "
                "(absent on this mailbox, or the lookup failed)"
            )
            continue
        folder_id = str(folder.get("id", ""))
        if folder_id:
            resolved[folder_id] = tag
    return resolved


def walk_graph_folders(
    fetch: GraphFetch,
    well_known: dict[str, str],
    *,
    segments: tuple[str, ...] = ("me", "mailFolders"),
    parent_id: str | None = None,
    depth: int = 0,
) -> list[Container]:
    """Every folder at ``segments`` and below, as exclusive ``Container`` rows.

    Recursive because the shape being walked IS a tree, and the depth bound is
    the recursion bound -- ``MAX_FOLDER_DEPTH`` caps it, so there is no
    unbounded stack.
    """
    if depth >= MAX_FOLDER_DEPTH:
        return []
    out: list[Container] = []
    for row in _graph_listing_rows(fetch, segments):
        folder_id = str(row.get("id", ""))
        out.append(
            graph_folder_to_container(
                folder_id, str(row.get("displayName", "")), parent_id, well_known
            )
        )
        if folder_id:
            out.extend(
                walk_graph_folders(
                    fetch,
                    well_known,
                    segments=("me", "mailFolders", folder_id, "childFolders"),
                    parent_id=folder_id,
                    depth=depth + 1,
                )
            )
    return out


def _graph_listing_rows(fetch: GraphFetch, segments: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every row of ONE folder collection, following ``@odata.nextLink`` to the end.

    Collected before the walk descends rather than interleaved with it, so the
    page loop and the depth recursion stay separate bounds that can each be
    read on their own.

    THE PAGING IS NOT OPTIONAL, and reading only the first page is the quiet
    failure this exists to remove: Graph pages a folder listing exactly as it
    pages messages, and a short answer is indistinguishable from a small
    mailbox. The caller gets fewer containers, no error, and no log line.

    The link is replayed VERBATIM -- never parsed, compared, normalised, or
    rebuilt -- which is the rule ``outlook.delta`` states for the delta feed and
    ``_folder_id`` already follows. Graph encodes its own paging state into the
    url in a form it is free to change, so a caller that reconstructed one would
    be asking for a position the service never issued.
    """
    rows: list[dict[str, Any]] = []
    page = fetch(*segments, operation="list_containers")
    for _ in range(MAX_FOLDER_PAGES):
        rows.extend(page.get("value", []) or [])
        next_link = page.get("@odata.nextLink")
        if next_link is None:
            return rows
        page = fetch(link=str(next_link), operation="list_containers")
    # BOUND REACHED, and reported rather than raised: the rows already proved
    # are truthful, and refusing the whole enumeration over a listing longer
    # than 10,000 folders would lose them. WARNING because unlike a missing
    # well-known alias this is NOT normal on any real mailbox -- it means the
    # answer is short, which is exactly the condition an operator must be able
    # to see in the log.
    logger.warning(
        f"CONTAINERS: folder listing stopped at the page bound segments={segments} "
        f"pages={MAX_FOLDER_PAGES} rows={len(rows)} (the result is INCOMPLETE)"
    )
    return rows


def gmail_listing_or_refuse(listing: Any, operation: str) -> dict[str, Any]:
    """A ``users.labels.list`` response, or ``ProviderError`` naming the caller.

    THE SHAPE CHECK THE ADAPTER USED TO SPELL ``dict(listing)`` (#186). That was
    a COERCION, not a guard, and it failed three ways at once on the one call
    both label readers share:

    * ``null``/``7`` raised ``TypeError: 'NoneType' object is not iterable`` and
      a JSON string raised ``ValueError: dictionary update sequence element #0
      has length 1; 2 is required``. Neither is ``ProviderError``, so both sail
      past every caller above the adapter boundary -- the same "error-contract
      leak" #186 is titled after.
    * WORSE, a JSON array of pairs raised nothing at all: ``[("labels", [...])]``
      coerces into a labels table, so the adapter FABRICATED routing entries out
      of a response Gmail never sent. A wrong answer is worse than a crash here,
      because ``list_containers`` runs on every watch round and on the reset
      snapshot path, and a fabricated container id is a filing destination.

    THE ``labels`` KEY IS CHECKED HERE TOO, and for the same two harms one
    level in (#186 residual). The envelope guard above left
    ``listing["labels"]`` -- the value both readers iterate at
    ``gmail_labels_to_containers`` and ``find_label_id`` -- unexamined, so a
    ``labels: 7``/``true``/``1.5`` raised ``TypeError: 'int' object is not
    iterable`` out of ``list_containers``, ``_label_id``, AND the 409-conflict
    re-list, and a ``labels: {...}``/``labels: "Label_9"`` raised nothing while
    reporting an EMPTY routing table for a mailbox that holds the label -- which
    then drove an unwarranted ``users.labels.create`` WRITE against the user's
    mailbox. That is the exact wrong-answer-plus-write this function's own
    argument above rules out for the envelope.

    ONE guard on the one shared read call, rather than a third check at each of
    the two iteration sites: two copies drift, and this call is already the
    single place both readers pass through.

    A MISSING KEY STAYS LEGAL, and ``null`` with it. Gmail OMITS ``labels``
    entirely on a mailbox with no labels, so refusing an absent key would refuse
    a correct response; the readers' existing ``or []`` already answers it.

    ``operation`` names the CALLER, matching the refusal ``_labels_listing``
    already raises for an ``HttpError``, so a shared call still produces a
    caller-specific message. The response body itself never ships: it is
    mailbox content, and its TYPE is what makes the refusal diagnosable.

    Lives here rather than in ``gmail/provider.py`` for the reason
    ``label_id_or_refuse`` does -- that file has no code-line budget left.
    """
    if not isinstance(listing, dict):
        logger.warning(
            f"CONTAINERS: gmail label listing is not an object operation={operation!r} "
            f"response_type={type(listing).__name__} "
            "(the listing is refused rather than coerced into a labels table)"
        )
        raise ProviderError(f"Gmail {operation} returned a label listing that is not an object")
    labels = listing.get("labels")
    if labels is not None and not isinstance(labels, list):
        logger.warning(
            f"CONTAINERS: gmail label listing's 'labels' key is not an array "
            f"operation={operation!r} labels_type={type(labels).__name__} "
            "(the listing is refused rather than read as an empty mailbox)"
        )
        raise ProviderError(
            f"Gmail {operation} returned a label listing whose 'labels' is not an array"
        )
    return listing


def gmail_labels_to_containers(listing: dict[str, Any]) -> list[Container]:
    """The whole ``users.labels.list`` response as ``Container`` rows.

    Gmail returns every label FLAT in one response, so unlike Graph there is no
    traversal and no depth bound: the mapping is the entire operation, which is
    why it is a pure function here rather than a method on the adapter.

    UNUSABLE ROWS ARE DROPPED, not raised on (#186) -- ``parse_gmail_label_row``
    decides which those are (#202) and this function decides what the drop
    MEANS here, which is: count it and say so.

    THE SKIP IS COUNTED AND SAID OUT LOUD AT WARNING (#186 residual). Each row's
    own line used to be DEBUG, which is off at the default level, so a listing
    whose rows were ALL unusable produced a silent empty routing table -- and an
    empty table is not inert: ``_label_id`` reads "no such label" off it and
    fires the create-once fallback, so the adapter issued a
    ``users.labels.create`` WRITE against a mailbox that already held the label
    (measured: ``labels: ["Label_9", 7, None]`` -> ``list_containers`` == ``[]``,
    one ``labels.create``). Dropping rows is still the right answer -- the
    healthy rows beside them are true, and refusing a whole routing table over
    one bad row is worse -- but doing it invisibly is not.

    ONLY COUNTS AND TYPES SHIP (#202). The rows themselves are mailbox content,
    and a malformed id is content like the rest of the row -- which is the same
    rule that stops the refusals below quoting a value. The TYPES are what makes
    the line diagnosable: "3 rows, all ``str``" and "3 rows, all ``dict``" are
    different failures.
    """
    rows = listing.get("labels", []) or []
    containers = [
        container for row in rows if (container := gmail_label_to_container(row)) is not None
    ]
    skipped = len(rows) - len(containers)
    if skipped:
        types = sorted({type(row).__name__ for row in rows})
        logger.warning(
            f"CONTAINERS: gmail label rows were unusable and skipped skipped={skipped} "
            f"of={len(rows)} kept={len(containers)} row_types={types} "
            "(an empty routing table drives a label create, so the skip is not silent)"
        )
    return containers


def find_label_id(listing: dict[str, Any], name: str) -> str | None:
    """The id of the label called ``name`` in a ``users.labels.list`` response.

    Matched on NAME rather than id, and correctly so: this resolves a TAXONOMY
    folder the installer configured, which exists only as a display name until
    Gmail mints an id for it. That is the opposite question from
    ``GMAIL_WELL_KNOWN``, which matches system labels by id precisely because
    their names are localised -- the two lookups are in the same module so the
    difference is visible instead of looking like an inconsistency.

    MATCHED CASE-INSENSITIVELY, because Gmail's own label-name uniqueness is:
    the API refuses to mint ``reference`` beside an existing ``Reference``. A
    case-sensitive match therefore does not merely miss the row -- it drives the
    caller's create-once fallback into a guaranteed ``409 Conflict`` on every
    single filing, so a mailbox whose label differs from the taxonomy by one
    capital letter can never be filed into at all.

    An EXACT match still wins over a case variant. Gmail cannot hold both rows,
    so this is defensive rather than load-bearing; stating it keeps the
    resolution deterministic instead of dependent on the listing's order.

    Returns None when no label carries that name, which the caller reads as
    "create it once".

    ONLY A ROW WITH A ``str`` NAME CAN MATCH (#202), which is the SAME name test
    ``parse_gmail_label_row`` applies, so a row that is not a mapping -- or whose
    ``name`` is ``None``, a number, or a list -- is simply not a candidate.
    ``str(label_name).casefold()`` used to run on whatever was there, so a row
    with ``name: None`` could case-match a taxonomy folder literally called
    "none". Reading ``.get`` off a bare string also raised ``AttributeError``
    (#186), a contract leak past every caller above the adapter.

    A NAME-MATCHING ROW WHOSE ID IS UNUSABLE RAISES ``ProviderError`` while
    ``gmail_labels_to_containers`` merely skips it, and that is the ONE
    judgement that legitimately differs between the two readers -- both use the
    same id test (``usable_label_id``, through ``label_id_or_refuse``), they
    just answer differently when it fails. Answering None here would say "no
    such label", and the caller reads that as "create it once": a
    ``users.labels.create`` WRITE against a mailbox that demonstrably HOLDS the
    label, ending in a 409 the conflict path cannot resolve. A refusal is the
    truthful answer AND the one that does not write. A container reader has no
    such write behind it, so a skip there costs one routing row.

    ``ProviderError`` rather than ``KeyError`` (#186): this function reads a raw
    provider payload, so a malformed response reaches it directly, and
    ``ProviderError`` is the type every caller above the adapter boundary
    already catches. The message names the label the CALLER asked for -- its own
    taxonomy string -- and never the row, whose remaining fields are mailbox
    content.

    THE VARIANT'S ID IS RESOLVED LAZILY, on the row finally chosen, which is
    what makes the exact-match promise above real: resolving it eagerly meant a
    malformed case variant positioned BEFORE a healthy exact match aborted a
    lookup that was fully resolvable.
    """
    folded = name.casefold()
    variant: dict[str, Any] | None = None
    for label in listing.get("labels", []) or []:
        label_name = label.get("name") if isinstance(label, dict) else None
        if label_name == name:
            return label_id_or_refuse(label, name)
        if variant is None and isinstance(label_name, str) and label_name.casefold() == folded:
            variant = label
    return None if variant is None else label_id_or_refuse(variant, name)


def label_id_or_refuse(label: Any, name: str) -> str:
    """The ``id`` of a matched Gmail label row, or ``ProviderError`` naming the label.

    THE WRITE-PATH GUARD, and the ONE place either branch reads an id (#186).
    ``find_label_id`` reads it off a listing row and
    ``GmailProvider._create_label`` reads it off a create response; the first
    pass of #186 guarded only the listing, and the create branch -- which fires
    on the ORDINARY case of a taxonomy label that does not exist yet -- still
    leaked ``KeyError: 'id'`` on a fresh install. Two guards drift, so there is
    one, and it lives here rather than in the adapter because
    ``gmail/provider.py`` is the file with no budget left.

    THE ID TEST IS ``usable_label_id`` (#202) -- literally the same call
    ``parse_gmail_label_row`` makes -- which is what stops this guard and the
    container reader from disagreeing about one row again. The difference
    between them is what they DO with an unusable id, not what counts as one:
    this returns the id or refuses, because the value is about to be WRITTEN to
    the mailbox, and a refusal is strictly safer than a silent wrong write. The
    harms are on the test itself; the short version is that ``""`` reached
    ``users.messages.modify`` as
    ``{'addLabelIds': [''], 'removeLabelIds': ['INBOX']}`` -- archiving the
    message while adding a label that does not exist -- and ``["Label_9"]``
    reached it as the literal ``"['Label_9']"``, both then cached and repeated
    on every later filing of that folder.

    A LABEL THAT IS NOT A MAPPING IS REFUSED, which is what makes the "one place
    either branch reads an id" claim above TRUE rather than dependent on the
    caller. The parameter is annotated ``Any`` DELIBERATELY and is not a
    weakening: it was ``dict[str, Any]``, which is a HINT on a function whose
    whole job is reading raw provider JSON, and nothing enforces a hint at
    runtime -- it only told ``mypy`` the check below was unreachable while the
    leak was live. The CREATE branch handed its response straight through, so
    ``users.labels.create`` answering ``"Label_9"``, ``null``, ``7``, or a JSON
    array leaked ``AttributeError: 'str' object has no attribute 'get'`` on the
    path that fires on every fresh install.

    THE VALUE ITSELF NEVER SHIPS in the message or the log. A malformed id is
    provider content like the rest of the row; its TYPE is what makes the
    refusal diagnosable, and the caller's own taxonomy name is what makes it
    actionable.
    """
    identifier = usable_label_id(label.get("id")) if isinstance(label, dict) else None
    if identifier is None:
        logger.warning(
            f"CONTAINERS: gmail label payload carried no usable id label_name={name!r} "
            f"payload_type={type(label).__name__} "
            f"id_type={type(label.get('id')).__name__ if isinstance(label, dict) else 'absent'} "
            "(the filing is refused rather than applied to nothing)"
        )
        raise ProviderError(f"Gmail returned a label named {name!r} with no id")
    return identifier
