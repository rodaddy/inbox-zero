"""WHICH FOLDERS THE WATCHER MAY READ. One question, one answer, one place.

Split out of ``config`` because it is a self-contained decision with its own
vocabulary and its own validators, and because the keystone was at its code-line
ceiling -- a limit that exists to catch exactly this kind of sprawl. ``config``
re-exports every name here, so callers import from either and the precedence
machinery is untouched.

THE PROBLEM THIS SOLVES
    The unattended watcher used to look only at Inbox, which is wrong in a real
    tenant: server-side Exchange rules move mail OUT of Inbox before the
    connector sees it, so an Inbox-only watcher silently misses whatever those
    rules touched. Widening it is where a connector in any business can do real
    damage -- shared mailboxes, client files, and confidential material are
    folders a client needs to be able to say NEVER about.

    So the model is a MODE plus two lists, matching what a client actually says
    out loud: Inbox only, everything, everything except X/Y/Z, or only X/Y/Z.
    A human answers once, the installer writes it down, and ``covers`` honours
    it forever. DETERMINISM-FIRST: no AI participates in this decision at any
    point, and ``covers`` is a pure function of the installed fields.

    One qualifier travels with those lists: whether container NAMES encode
    hierarchy (``names_encode_nesting``). Gmail's do and Graph's do not, so a
    client excluding ``Clients`` on Gmail means ``Clients/Smith`` too, while
    the same string on Graph names one folder. The field records which mailbox
    the answer was given about; nothing infers it from a provider name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from pydantic import field_validator, model_validator

from inbox_zero_connector.utils.sections import Section

MAX_SCOPE_FOLDERS: Final = 200
"""Upper bound on either folder list in ``MailboxScope``.

A mailbox scope is a list a human read aloud and agreed to, so the realistic
size is single digits and a large firm's shared-mailbox tree is dozens. 200 is
far above any answered interview and far below a machine-generated dump: the
ceiling exists so a corrupted or programmatically-appended config cannot turn
the reviewed exclusion list into an unbounded one that nobody has actually
read. Exceeding it means the list stopped being a human decision, which is the
condition this kit refuses to run under.
"""

NESTING_SEPARATOR: Final = "/"
"""The character Gmail renders as label nesting. A DISPLAY convention, not an API one.

Gmail's ``Label`` resource carries no parent field: ``Clients/Smith`` is one
independent label with its own id whose name merely happens to contain a
slash, and the Gmail UI draws it underneath ``Clients``. So the only place
that hierarchy exists is the name, and honouring the client's answer means
reading it out of the name -- see ``MailboxScope.names_encode_nesting`` for
why that is a per-install decision rather than something applied everywhere.

Named rather than inlined because it appears in a boundary test, in a prefix
construction, and in the docstrings that justify both. An inline ``"/"`` in
three places is three chances for one of them to become something else.
"""

WELL_KNOWN_JUNK: Final = "junk"
WELL_KNOWN_DELETED: Final = "deleted"
WELL_KNOWN_ARCHIVE: Final = "archive"
WELL_KNOWN_INBOX: Final = "inbox"

SCOPE_MODES: Final[tuple[str, ...]] = (
    "inbox_only",
    "all_folders",
    "all_except",
    "only_listed",
)

ScopeMode = Literal["inbox_only", "all_folders", "all_except", "only_listed"]


class MailboxScope(Section):
    """WHICH FOLDERS THE WATCHER MAY LOOK AT. The single scope decision point.

    The unattended watcher used to look only at Inbox, and that is wrong for a
    real deployment: server-side Exchange rules move mail OUT of Inbox before
    the connector ever sees it, so an Inbox-only watcher silently misses the
    mail those rules touched. Widening it, though, is the exact place a
    connector in any business can do real damage -- shared mailboxes, client
    files, and confidential material are folders that must NEVER be read, and
    "never" has to be expressible as data the client named.

    So this is a MODE plus two lists, matching what a client actually says in
    the interview: look at Inbox only, look at everything, look at everything
    except X/Y/Z, or look at only X/Y/Z. The human answers once, the installer
    writes it down, and ``covers`` honours it forever. No AI participates in
    this decision at any point -- ``covers`` is a pure function of the
    installed fields and the folder name in front of it.

    DEFAULT IS ``inbox_only`` ON PURPOSE. It is the behaviour every existing
    install already has, so upgrading a deployment that never heard of this
    model cannot silently widen what the connector reads. A scope default that
    broadened on upgrade would be a data-exposure change delivered by a version
    bump, which nobody consented to.

    The three ``include_*`` booleans are separate from ``excluded_folders``
    because Junk, Deleted Items, and Archive are well-known system folders
    rather than folders the owner named. A client who says "all folders" is
    answering about their filing tree; they almost never mean "and my Deleted
    Items", and requiring them to have thought of spelling those three by hand
    would make the common answer wrong by omission. Each defaults to False, so
    the widest mode still excludes all three until somebody says otherwise.
    """

    mode: ScopeMode = "inbox_only"
    """The client's answer. Default is the pre-existing Inbox-only behaviour."""

    excluded_folders: tuple[str, ...] = ()
    """Folders never read. Meaningful when ``mode`` is ``all_except``."""

    included_folders: tuple[str, ...] = ()
    """The only folders read. Meaningful when ``mode`` is ``only_listed``."""

    include_junk: bool = False
    include_deleted: bool = False
    include_archive: bool = False

    names_encode_nesting: bool = False
    """Whether a container NAME encodes hierarchy with ``NESTING_SEPARATOR``.

    True for Gmail, False for Microsoft Graph, and the difference is real
    rather than cosmetic. A Gmail label named ``Clients/Smith`` is an
    independent label whose only relationship to ``Clients`` is the slash a
    human typed; the API reports no parent. A Graph folder genuinely nests via
    ``parentFolderId`` and its display name does NOT encode that -- a Graph
    folder literally named ``Clients/Smith`` is ONE folder whose name contains
    a slash, and treating it as a child of a folder called ``Clients`` would
    invent a relationship the provider never stated.

    WHY A FIELD ON THE SCOPE, and not a per-call argument. ``covers`` is the
    single scope question and nothing may re-derive scope from the raw fields;
    a flag passed at each call site is a decision made separately by every
    caller, and the caller that forgets it silently gets flat matching -- which
    is the direction that READS the excluded child. Installed once beside the
    mode and the lists, it travels with the answer it qualifies and no caller
    can drop it. The scope is already keyed per provider in ``ConnectorConfig``,
    so the installer has exactly the right seam to set it.

    WHY NOT ``Container.exclusive``, which is the other flag that happens to
    split these two providers the same way. Exclusivity is about how many
    containers ONE MESSAGE holds; name-encoded nesting is about how one
    container's NAME relates to another's. They are independent properties that
    coincide across Gmail and Graph, and binding the second to the first would
    make a future exclusive-container provider with slash-delimited names
    silently wrong in the reading direction. This field states the property it
    actually means.

    DEFAULT IS False for the same reason ``mode`` defaults to ``inbox_only``:
    it is the behaviour every existing install already has, so an upgrade
    cannot change what a deployment reads. False is also the conservative
    direction for INCLUSION (a listed parent admits only itself) while being
    the permissive one for EXCLUSION -- which is why the Gmail installer must
    set it True rather than leaving it to the default.
    """

    @field_validator("excluded_folders", "included_folders")
    @classmethod
    def _clean_folder_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Strip each name, refuse an empty one, and bound the list length."""
        names = tuple(item.strip() for item in value)
        if any(not name for name in names):
            raise ValueError(
                "mailbox_scope folder lists must not contain an empty name. "
                "ACTION REQUIRED: remove the blank entry -- an empty string would "
                "match nothing and silently drop a folder the client named."
            )
        if len(names) > MAX_SCOPE_FOLDERS:
            raise ValueError(
                f"mailbox_scope names {len(names)} folders; the limit is "
                f"{MAX_SCOPE_FOLDERS}. ACTION REQUIRED: shorten the list -- a list "
                "that long was not read by the human who is accountable for it."
            )
        return names

    @model_validator(mode="after")
    def _mode_has_its_list(self) -> MailboxScope:
        """A list-driven mode with an empty list is a config ERROR, never a default.

        ``all_except`` with nothing excluded would read as ``all_folders``, and
        ``only_listed`` with nothing included would read as "no folders". Both
        mean the human was asked which folders to leave alone and answered
        nothing. Guessing either way is wrong: one silently reads the shared
        client mailbox, the other silently watches nothing while reporting
        success. Fail at load, name the field, and make somebody answer.
        """
        if self.mode == "all_except" and not self.excluded_folders:
            raise ValueError(
                "mailbox_scope.mode is 'all_except' but excluded_folders is empty. "
                "ACTION REQUIRED: name the folders to exclude, or set mode to "
                "'all_folders' if the client really did mean every folder. An empty "
                "exclusion list must not quietly become 'read everything'."
            )
        if self.mode == "only_listed" and not self.included_folders:
            raise ValueError(
                "mailbox_scope.mode is 'only_listed' but included_folders is empty. "
                "ACTION REQUIRED: name the folders to watch, or set mode to "
                "'inbox_only'. An empty inclusion list would watch nothing while "
                "reporting a healthy watcher."
            )
        overlap = sorted({
            name for name in self.included_folders if name.casefold() in self._excluded_keys()
        })
        if overlap:
            raise ValueError(
                f"mailbox_scope names the same folder in both lists: {', '.join(overlap)}. "
                "ACTION REQUIRED: put each folder in exactly one list -- a folder that "
                "is both included and excluded has no defensible answer, and picking "
                "one would make a privileged folder's fate depend on our tie-break "
                "rather than the client's instruction."
            )
        return self

    def _excluded_keys(self) -> frozenset[str]:
        """Case-folded exclusion names. See ``covers`` for why case-folded."""
        return frozenset(name.casefold() for name in self.excluded_folders)

    def _included_keys(self) -> frozenset[str]:
        """Case-folded inclusion names."""
        return frozenset(name.casefold() for name in self.included_folders)

    def _named_by(self, key: str, names: frozenset[str]) -> bool:
        """Whether ``key`` is one of ``names``, or lives beneath one of them.

        THE PARENT'S ANSWER GOVERNS THE SUBTREE, in both directions. Excluding
        ``Clients`` excludes ``Clients/Smith``; including ``Clients`` includes
        it. Both readings come from the same sentence a client actually says:
        a business that points at ``Clients`` and says "never" means the client
        folders inside it, and one that points at the same label and says "watch
        this" means the same folders. Splitting the two -- exclusion cascading
        while inclusion did not -- would make a label's fate depend on which
        list it was written in rather than on what the client said.

        The subtree test is a BOUNDARY match, never a bare ``startswith``.
        ``Clients/Smith`` is a child of ``Clients`` because the separator
        follows the parent exactly; ``MyClients`` and ``Clients-Archive`` are
        not children of anything, and a substring or bare-prefix test would
        exclude or include both of them. That is the specific bug this shape
        exists to refuse: it reads as working on the happy path and silently
        captures unrelated labels, which in the exclusion direction hides mail
        the client asked for and in the inclusion direction reads mail they
        did not name.

        When ``names_encode_nesting`` is False the prefix set is empty, so a
        Graph mailbox gets exactly the whole-name equality it had before -- a
        Graph folder whose display name contains a slash is one folder, and
        nothing here pretends otherwise.
        """
        prefixes = tuple(f"{name}{NESTING_SEPARATOR}" for name in names)
        return key in names or (self.names_encode_nesting and key.startswith(prefixes))

    def well_known_tag(self, folder_name: str, *, well_known: str | None = None) -> str | None:
        """Resolve a container's well-known tag. THE single resolution, one place.

        Provider tag first, display name second, and both spellings of that
        sentence live here rather than at each caller. ``covers`` asks this,
        the entry guard asks this through ``covers``, and ``refusal`` asks it
        to name the switch -- so no caller can hold a different opinion about
        what kind of folder it is looking at.

        THAT DIVERGENCE WAS THE DEFECT (#201.1). ``loop.py`` read
        ``container.well_known`` directly to name the ``include_*`` switch,
        while ``covers`` had already refused the same container through the
        display-name fallback. On Gmail the two disagree on the ordinary case:
        ``GMAIL_WELL_KNOWN`` has no archive key, so a Gmail label named
        ``Archive`` arrives tagged ``None``, ``covers`` refuses it as
        ``archive`` from its name, and the WARN could only offer a generic
        "system-folder settings" sentence -- naming no switch for a folder the
        scope had refused by a switch. One resolution makes that unreachable.

        THE PROVIDER TAG WINS when there is one, because a localised mailbox
        spells "Deleted Items" in the owner's language and the adapter knows
        the folder id regardless. The display-name table is the fallback for
        the containers a provider does not tag at all, which on Gmail is every
        user label -- including one a human named ``Archive``.

        Args:
            folder_name: The folder's display name as the provider reports it.
            well_known: The provider's own tag, when it has one.

        Returns:
            ``"junk"``, ``"deleted"``, ``"archive"``, ``"inbox"``, or None for
            a container that is not a well-known system folder.
        """
        if well_known:
            return well_known.strip().casefold()
        return _WELL_KNOWN_DISPLAY_NAMES.get(folder_name.strip().casefold())

    def refusal(self, folder_name: str, *, well_known: str | None = None) -> str | None:
        """WHICH SWITCH, IF ANY, REFUSED THIS CONTAINER. The reason behind ``covers``.

        ``covers`` answers yes or no; this answers why not, and both read the
        SAME ``well_known_tag`` so they cannot name different folders. A
        caller that needs to tell an operator what to edit asks this instead of
        re-deriving the answer from a provider field -- which is precisely the
        re-derivation that shipped the wrong sentence (#201.1).

        ONLY THE WELL-KNOWN SWITCHES ARE NAMED, because they are the only
        refusal with a single named field behind it. A container refused by the
        MODE is refused by a mode plus a list, and there is no one setting to
        point at; the caller already knows the mode and says so itself.

        Args:
            folder_name: The folder's display name as the provider reports it.
            well_known: The provider's own tag, when it has one.

        Returns:
            The config field that refused it -- ``"include_junk"``,
            ``"include_deleted"``, or ``"include_archive"`` -- or None when no
            well-known switch is what refused it (including when the container
            is covered).
        """
        if self.covers(folder_name, well_known=well_known):
            return None
        tag = self.well_known_tag(folder_name, well_known=well_known)
        if tag in _WELL_KNOWN_SWITCHES:
            return _WELL_KNOWN_SWITCHES[tag]
        return None

    def covers(self, folder_name: str, *, well_known: str | None = None) -> bool:
        """Whether the watcher may read ``folder_name``. THE single scope question.

        Every caller asks this and nothing re-derives scope from the raw
        fields, because a second implementation of this decision is a second
        chance to read an excluded folder.

        MATCHING IS CASE-INSENSITIVE, and that is a security property rather
        than a convenience. Outlook folder names are not case-sensitive to the
        people who name them: a client who writes "Client Matters" in the
        interview and whose mailbox actually spells it "Client matters" has
        given one instruction, not two. A case-sensitive comparison would miss
        that exclusion and read the folder anyway -- a silent data-exposure
        bug with no error, no log line, and no way for the client to notice
        before the content was already in a summary. Casefolded comparison
        makes the miss impossible in that direction.

        NESTED CONTAINERS ARE GOVERNED BY THEIR PARENT when the install says
        names encode nesting -- excluding ``Clients`` excludes
        ``Clients/Smith``, and including it includes it. The mechanism, the
        boundary rule that keeps ``MyClients`` out of it, and the reason this
        is an installed field rather than a per-call argument are all in
        ``_named_by`` and ``names_encode_nesting``. It applies to the two
        list-driven modes only; ``inbox_only`` and ``all_folders`` name no
        folders, so there is no parent to inherit from.

        The well-known system folders are decided BEFORE the mode, so their
        booleans hold in every mode including ``all_folders``. A caller that
        knows a folder is Junk/Deleted/Archive passes ``well_known``; a caller
        that only has a display name gets the same answer for the standard
        English spellings.

        Args:
            folder_name: The folder's display name as the provider reports it.
            well_known: Optional provider-independent tag -- ``"junk"``,
                ``"deleted"``, ``"archive"``, or ``"inbox"``. Preferred over
                display-name guessing, because a localised mailbox spells
                "Deleted Items" in the owner's language.

        Returns:
            True when the watcher may read this folder.
        """
        key = folder_name.strip().casefold()
        tag = self.well_known_tag(folder_name, well_known=well_known)

        if tag == WELL_KNOWN_JUNK:
            return self.include_junk
        if tag == WELL_KNOWN_DELETED:
            return self.include_deleted
        if tag == WELL_KNOWN_ARCHIVE:
            return self.include_archive

        if self.mode == "inbox_only":
            return tag == WELL_KNOWN_INBOX
        if self.mode == "all_folders":
            return True
        if self.mode == "all_except":
            return not self._named_by(key, self._excluded_keys())
        return self._named_by(key, self._included_keys())

    def entry_names(self, entry: str, folder_name: str) -> bool:
        """Whether ONE list entry names ``folder_name``, directly or as its child.

        THE PUBLIC FORM OF ``_named_by`` FOR A SINGLE ENTRY, and it exists
        because a caller needed exactly this and had no honest way to ask.
        ``loop.py`` reached past the underscore into ``_named_by`` with a
        one-element frozenset it built itself, documenting that as "the smaller
        wrong" against a private copy that could rot. Both options were bad;
        the missing thing was this method. A caller asking which entry is
        responsible for a container gets the same casefolding, stripping, and
        nesting-boundary rule ``covers`` uses, because it is the same code.

        THIS IS NOT A SCOPE ANSWER and must never be used as one. It answers
        about ONE list entry in isolation, which is only half the question --
        the well-known switches are decided before the mode, so an entry can
        name a container the scope still refuses. ``covers`` is the answer;
        this identifies which entry to talk about. Asking only this one was the
        original defect: an entry naming ``Archive`` matched, suppressed its
        own warning, and was refused a stream anyway.

        Args:
            entry: One name out of ``included_folders`` or ``excluded_folders``.
            folder_name: The folder's display name as the provider reports it.

        Returns:
            True when ``entry`` names that folder, or names its parent under
            ``names_encode_nesting``.
        """
        return self._named_by(folder_name.strip().casefold(), frozenset({entry.strip().casefold()}))

    def covers_all(
        self,
        names: Sequence[str],
        *,
        well_known: Sequence[str | None] = (),
    ) -> bool:
        """Whether the watcher may read a message carrying ALL of ``names`` at once.

        ``covers`` answers about ONE container, which is the whole truth on
        Microsoft Graph: a message lives in exactly one folder, so one question
        has one answer. Gmail does not work that way -- a message carries many
        labels simultaneously, and a message labelled both ``Billing`` and
        ``Client Matters`` is a normal, uncontrived mailbox state. Asking
        ``covers`` once per label and combining the answers ad hoc in each
        caller is how an excluded label gets read: whichever caller is written
        first picks a combining rule by accident, and the accident is invisible
        because nothing errors and nothing logs.

        THE RULE IS ANY-EXCLUSION-WINS, and it is a deliberate choice rather
        than a fallout of the implementation. If ANY container the message
        carries is outside scope, the message is NOT covered -- no number of
        in-scope containers can outvote a single out-of-scope one. The only
        alternative worth naming is any-inclusion-wins, and it is rejected on
        purpose: it would read exactly the mail a client asked us to stay out
        of, because the way confidential mail actually appears is a client email
        that ALSO carries a benign label. A client who says "never read Client
        Matters" is not saying "unless it is also Billing", and a rule that
        reads it anyway breaks the promise in the one case the promise was
        made for. When a mailbox may hold confidential material, the conservative
        direction is the only safe one; a false refusal costs a summary the
        client can ask for by hand, a false read cannot be taken back.

        AN EMPTY SEQUENCE IS NOT COVERED. A message with no containers at all
        is a message we know nothing about, and "we know nothing" must not
        resolve to permission. This also makes the degenerate provider bug --
        a label list that failed to populate -- fail closed rather than open up
        the whole mailbox.

        Matching is case-insensitive across every element, for the same reason
        ``covers`` casefolds: see its docstring, which owns that argument. The
        nested-container rule reaches here for free, because every element is
        answered by ``covers`` -- so a message carrying ``Clients/Smith`` is
        refused by an exclusion naming ``Clients``, which is the combination
        the Gmail defect actually presents as.

        Args:
            names: Every container the message carries. Gmail label display
                names, or the single folder name on Graph.
            well_known: Optional provider-independent tags positionally paired
                with ``names``. A shorter sequence pads with ``None``, so a
                caller that can tag only some containers passes what it knows.

        Returns:
            True only when EVERY container is individually covered.
        """
        if not names:
            return False
        padding: tuple[str | None, ...] = (None,) * len(names)
        tags = tuple(well_known)[: len(names)] + padding
        return all(self.covers(name, well_known=tags[index]) for index, name in enumerate(names))


_WELL_KNOWN_DISPLAY_NAMES: Final[dict[str, str]] = {
    "inbox": WELL_KNOWN_INBOX,
    "junk": WELL_KNOWN_JUNK,
    "junk email": WELL_KNOWN_JUNK,
    "spam": WELL_KNOWN_JUNK,
    "deleted": WELL_KNOWN_DELETED,
    "deleted items": WELL_KNOWN_DELETED,
    "trash": WELL_KNOWN_DELETED,
    "archive": WELL_KNOWN_ARCHIVE,
}
"""Standard English display names for the folders every mailbox has.

A fallback only. It is deliberately small and covers exactly the spellings
Gmail and Exchange Online use in an English mailbox; a localised mailbox is
handled by the caller passing ``well_known`` explicitly, which is why that
argument exists rather than this table growing per-language rows.
"""


_WELL_KNOWN_SWITCHES: Final[dict[str, str]] = {
    WELL_KNOWN_JUNK: "include_junk",
    WELL_KNOWN_DELETED: "include_deleted",
    WELL_KNOWN_ARCHIVE: "include_archive",
}
"""Which config field decides each well-known tag. Read by ``refusal`` only.

``inbox`` is absent on purpose: there is no ``include_inbox``. The Inbox is
decided by the MODE, so a refused Inbox has no single field to point an
operator at and ``refusal`` correctly returns None for it.

A table rather than three branches so the three switches stay one fact in one
place -- the branch tree in ``covers`` reads them in the same order and adding
a fourth system folder is a row here plus a field, not a control change.
"""


def default_scope(*, names_encode_nesting: bool = False) -> MailboxScope:
    """Inbox only, no system folders. The behaviour every existing install has.

    ``names_encode_nesting`` is a keyword because it is the one property of
    this default that is not universal: it describes the MAILBOX the answer
    was given about, so the caller that knows which mailbox that is supplies
    it. It stays a plain boolean argument rather than a provider name -- this
    module has no provider vocabulary and must not grow one, or the field
    becomes something inferred from a vendor string instead of something
    recorded about a mailbox.

    It defaults to False here, matching the field, so an existing caller with
    no opinion gets byte-identical behaviour. The per-provider answers live in
    ``config.DEFAULT_SCOPES``, which is the seam that already knows the
    provider key.
    """
    return MailboxScope(names_encode_nesting=names_encode_nesting)
