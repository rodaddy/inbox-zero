"""The closed folder vocabulary.

Security invariant 2: email content is UNTRUSTED INPUT. The host AI reads
attacker-controlled text and then tells this connector where to file a
message. If the connector accepted whatever label the AI produced, a hostile
email could talk the AI into creating folders, exfiltrating structure, or
hiding itself somewhere the owner never looks.

It does not. The taxonomy is fixed at install time and this module rejects
anything outside it. A hostile email's worst case is misfiling ITSELF into one
of a handful of folders the owner already knows about -- fully reversible, and
receipted to the ledger either way.

The AI cannot widen the vocabulary at runtime. There is no add-folder tool,
and this module offers no writer beyond first-time installation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from inbox_zero_connector.errors import OperatorRefusalError
from inbox_zero_connector.utils import write_text_atomic
from inbox_zero_connector.utils.paths import taxonomy_path

DEFAULT_FOLDERS: tuple[tuple[str, str], ...] = (
    (
        "Business-Action",
        "Work mail the owner must personally answer or decide. File here only "
        "when the next move is the owner's: a question addressed to them, an "
        "approval, a decision, a deadline they own. If the next move belongs to "
        "someone else, it is Business-Waiting.",
    ),
    (
        "Business-Waiting",
        "Work mail where the owner has already done their part and is blocked "
        "on someone else's reply, delivery, or signature. The distinguishing "
        "test against Business-Action is whose court the ball is in: here it is "
        "not the owner's, so the only pending act is a follow-up if the other "
        "side goes quiet.",
    ),
    (
        "Business-Reference",
        "Work mail worth keeping with nothing to do: confirmations of things "
        "already settled, documents and policies to look up later, FYI threads "
        "the owner was copied on. If reading it produces no task for anyone, it "
        "belongs here rather than in either Action folder.",
    ),
    (
        "Personal-Action",
        "Non-work mail that needs a reply or an act from the owner -- family, "
        "friends, doctors, schools, personal accounts and admin. Same "
        "next-move-is-mine test as Business-Action; the split is only whether "
        "the matter is the owner's business or the owner's life.",
    ),
    (
        "Personal-Reference",
        "Non-work mail worth keeping with nothing to do: travel confirmations, "
        "appointment details, personal records, correspondence to keep. "
        "Anything here that later needs an act belongs in Personal-Action "
        "instead.",
    ),
    (
        "Receipts",
        "Proof of a transaction: invoices, order confirmations, shipping "
        "notices, statements, payment and renewal receipts, business or "
        "personal alike. Receipts wins over the Reference folders when the "
        "message records money moving, because that is what the owner searches "
        "for at tax time. A bill that is unpaid and due is an Action, not a "
        "receipt.",
    ),
    (
        "Newsletters",
        "Bulk mail the owner deliberately subscribed to and would notice the "
        "absence of: publications, digests, product updates from tools in use, "
        "communities joined. The test against Not-Interested is consent -- the "
        "owner asked for this, so it is filed rather than dismissed.",
    ),
    (
        "Not-Interested",
        "Bulk or targeted mail the owner never asked for and does not want: "
        "cold sales outreach, recruiter spray, vendor pitches, promotions from "
        "one-time purchases, conference and webinar invitations out of the "
        "blue. Unwanted, but not deleted -- filing it here keeps the inbox "
        "clear while leaving it findable if the owner turns out to want it.",
    ),
    (
        "Review",
        "The uncertainty bucket. File here when the message does not clearly "
        "fit one of the folders above -- ambiguous purpose, mixed personal and "
        "business, an unknown sender who might matter, anything the classifier "
        "would otherwise be guessing at. Choosing this is a correct answer, "
        "never a failure: a classifier forced to guess either buries what "
        "mattered or floods Action with noise, and either one teaches the owner "
        "to stop trusting the system. Saying 'I do not know' here is the same "
        "instinct as the connector refusing to send.",
    ),
)
"""Starting vocabulary. The installer interview may replace these entirely.

Nine folders, split business/personal at the top because that is the division
owners already make in their heads, and every one of them earns its place by
changing what the owner does next.

Deliberately still small: a large taxonomy pushes classification accuracy down
and gives a prompt-injection attempt more room to aim. ``Review`` is the one
folder that exists for the classifier rather than the owner -- without an
explicit place to be unsure, an uncertain message has to be forced into a
confident answer, and both available wrong answers cost trust.
"""

MAX_LOGGED_FOLDER_CHARS = 40
"""How much of a REJECTED folder name reaches the log. See ``Taxonomy.validate``.

A valid folder name is short by construction (the taxonomy caps at 24 of them
and they are human filing labels). Anything longer is not a folder name, and
truncating is what stops an injected string from being copied into the log
verbatim.
"""

MAX_FOLDERS = 24
"""Upper bound on taxonomy size, enforced at load.

Not a security control by itself -- it is a guard against a corrupted or
hand-edited config turning the closed vocabulary into an effectively open one.
"""

DEFAULT_REVIEW_FOLDER: Final = "Review"
"""The conventional name of the uncertainty folder, and the implicit marker.

A taxonomy that contains a folder called ``Review`` is DECLARING that folder as
its uncertainty bucket without having to say so, which is what keeps every
already-installed taxonomy file valid across this change. Rename it and the
taxonomy must say which folder took the job -- see ``REVIEW_FOLDER_KEY``.
"""

REVIEW_FOLDER_KEY: Final = "review_folder"
"""The top-level JSON key that names the uncertainty folder explicitly.

The explicit marker WINS over the ``Review``-by-name convention, so a taxonomy
that keeps a folder literally called ``Review`` for some other purpose can still
point the uncertainty role at a different one. Its value must be a folder that
exists in the same file; a marker naming an absent folder is the exact defect
this key was added to stop, so it fails at load rather than at filing time.
"""


# The three non-ASCII entries are spelled as escapes on purpose: they LOOK like
# an ordinary semicolon in every editor, which is the whole reason they are on
# the blocklist, and a literal here would be indistinguishable from the ASCII
# one on the line above (ruff RUF001 flags exactly this).
FORBIDDEN_LABEL_CHARS: Final[frozenset[str]] = frozenset({
    ",",  # U+002C COMMA -- Outlook category-list delimiter
    ";",  # U+003B SEMICOLON -- Outlook category-list delimiter
    "\u061b",  # ARABIC SEMICOLON
    "\ufe54",  # SMALL SEMICOLON
    "\uff1b",  # FULLWIDTH SEMICOLON
})
"""Characters that must never appear in a provider label / Outlook category name.

THE REASON MATTERS MORE THAN THE RULE, because the rule looks arbitrary and the
reason is what stops someone relaxing it: comma and semicolon are the
delimiters Outlook uses to SERIALISE a multi-category list into one string. A
category name containing one does not fail loudly -- it SPLITS. Write
``Business, Action`` and the mailbox reads back the two categories ``Business``
and ``Action``, neither of which is what we wrote. Nothing errors and nothing
logs, so our ledger receipt claims a classification the message does not carry.

That silence is exactly why this is an assertion rather than a comment. A split
is only visible if you read the categories back and compare, so a name
containing a comma will look like it "obviously works" in whatever test the
next person runs. It does not.

The three extra codepoints are the international semicolon variants Outlook's
own client-side validation rejects alongside the ASCII pair: Arabic semicolon
(U+061B), small semicolon (U+FE54), fullwidth semicolon (U+FF1B). They are here
because they are on Microsoft's blocklist, not because this product would
generate one -- a name pasted into a config file from a non-Latin locale can
carry one invisibly.

Source: the category-rendering research brief, 2026-08-18, section 5. Confidence
``[MS-COMMUNITY]`` -- a Microsoft Q&A respondent extracted the list from
Outlook's client-side JavaScript. Colon and hyphen are on that source's
EXPLICITLY SUPPORTED list, which is what makes ``rTech: Business-Action`` legal
by design rather than by luck.
"""

MAX_LABEL_CHARS: Final = 64
"""Our ceiling on a label name, deliberately far below the reported provider one.

The reported Outlook maximum is 255 characters -- but that figure is
``[MS-COMMUNITY]`` and UNVERIFIED at the vendor level: no primary Microsoft
reference doc stating it was found (research brief section 5). Validating right
up against an unverified limit means that if it turns out to be 250, or to count
UTF-16 units rather than codepoints, we find out in a client's mailbox.

64 costs nothing. A label is a human filing affordance read in a message list,
and the longest name this product would ever produce
(``rTech: Business-Reference``, 26 characters) leaves more than double the
headroom. Anything approaching 64 is a sentence, which means the caller is
passing the wrong value entirely -- so this ceiling catches OUR bug long before
it ever approaches Microsoft's boundary, which is the useful property.
"""


class TaxonomyError(OperatorRefusalError):
    """Raised when the taxonomy is missing, malformed, or violated.

    An ``OperatorRefusalError`` for the same reason ``ConfigError`` is one: the
    message it carries is already a complete instruction to the person who
    installed the kit ("Run 'uv run inbox-zero-connector init-taxonomy'"), and a
    missing taxonomy is the ordinary state of an install that has not finished
    ``setup`` -- not a defect with a stack worth reading. Found standing in the
    #195 manual proof, where ``watch --once`` against a fresh config directory
    reached this before it ever reached the provider and printed three
    tracebacks over the one line that told the operator what to run.
    """


class UnknownFolderError(TaxonomyError):
    """Raised when a caller asks to file a message into a folder not in the taxonomy.

    This is the invariant-2 rejection. It carries the valid set so the host AI
    can retry with a legal value instead of guessing again.
    """

    def __init__(self, requested: str, allowed: tuple[str, ...]) -> None:
        """Build the refusal, keeping both values reachable as attributes.

        The message spells out the whole allowed set and states that the
        taxonomy cannot be extended at runtime, because its reader is the host
        AI: a refusal that only says "no" invites another guess, whereas one
        that names the legal values ends the loop in a single retry.
        ``requested`` and ``allowed`` are also kept as attributes so a caller
        can build its own response without re-parsing the message text.

        Args:
            requested: The folder the caller asked for. Attacker-influencable
                -- callers logging it must truncate (see
                ``Taxonomy.validate_folder``).
            allowed: The closed vocabulary in effect.
        """
        self.requested = requested
        self.allowed = allowed
        super().__init__(
            f"Folder {requested!r} is not in this mailbox's taxonomy. "
            f"Allowed folders: {', '.join(allowed)}. "
            "The taxonomy is fixed at install time and cannot be extended at runtime."
        )


class LabelNameError(TaxonomyError):
    """Raised when a proposed provider label / category name is unusable.

    Distinct from ``UnknownFolderError`` on purpose. That one refuses a folder
    outside the closed vocabulary -- a POLICY decision this product makes. This
    one refuses a name the PROVIDER would mangle -- a MECHANICAL fact about
    Outlook. A name can be perfectly in-vocabulary and still fail here, and
    conflating the two would let a future change to our policy look like it
    loosened a provider constraint.
    """


def validate_label_name(name: str) -> str:
    """Return the cleaned label name, or raise ``LabelNameError`` explaining why not.

    Call this on any name headed for a provider's category/label write path,
    BEFORE the write. Nothing in the connector writes categories yet -- this is
    the constraint standing ready for the lane that does.

    Surrounding whitespace is STRIPPED before validation, and the stripped form
    is what is returned. A trailing space is invisible in a config file or a
    JSON string, so ``"Business-Action "`` would otherwise produce a category
    that looks identical to the valid one in every UI while being a different
    string to every comparison, filter, and rule. Silently correcting it is
    right here because there is no legitimate reason to want the space and no
    way for the owner to see that they have one.

    Every rejection names the offending character and its consequence: the
    message is read by a host AI that has no other channel to learn the
    remediation, and "invalid name" teaches it to guess.
    """
    cleaned = name.strip()
    if not cleaned:
        raise LabelNameError(
            f"Label name {name!r} is empty or only whitespace. A category must have a "
            "name the mailbox owner can read in their message list."
        )

    offenders = sorted(FORBIDDEN_LABEL_CHARS & set(cleaned))
    if offenders:
        rendered = ", ".join(f"{char!r} (U+{ord(char):04X})" for char in offenders)
        raise LabelNameError(
            f"Label name {cleaned!r} contains forbidden character(s): {rendered}. "
            "Outlook uses comma and semicolon to separate the categories on a message, "
            "so a name containing one is SPLIT into two separate categories on write -- "
            "silently, with no error, producing labels nobody chose. "
            "Use a colon or a hyphen instead; both are supported."
        )

    if len(cleaned) > MAX_LABEL_CHARS:
        raise LabelNameError(
            f"Label name is {len(cleaned)} characters, over this connector's "
            f"{MAX_LABEL_CHARS}-character ceiling. A label is a short filing word read in "
            "a message list; a value this long is almost certainly the wrong argument."
        )

    return cleaned


class Taxonomy(BaseModel):
    """An immutable closed vocabulary of folder names, one of which absorbs doubt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    folders: tuple[str, ...]
    descriptions: dict[str, str]
    review_folder: str = DEFAULT_REVIEW_FOLDER
    """Which folder receives everything the connector is not sure about.

    THE TAXONOMY OWNS THIS NAME, not the decision layer. ``decide`` used to
    spell ``"Review"`` as its own constant, which meant an installer interview
    that renamed the uncertainty folder produced a connector coercing mail into
    a folder the mailbox did not have -- the decision looked fine and the filing
    failed, one layer further on and long after the config that caused it.

    Defaulted rather than required so that a taxonomy which keeps the
    conventional ``Review`` name -- every one shipped so far -- constructs
    unchanged. The validator below is what makes the default honest: it refuses
    a taxonomy whose declared uncertainty folder is not one of its own folders,
    so the failure lands at LOAD, in front of the operator holding the file.
    """

    @model_validator(mode="after")
    def _uncertainty_folder_must_exist(self) -> Taxonomy:
        """Refuse a vocabulary with nowhere to put "I do not know".

        Raised at construction, which is load time for every real caller. The
        message names the two ways to fix it because its reader is an operator
        with a hand-edited or interview-generated file, and "invalid taxonomy"
        tells them nothing about which line to change.

        Raises:
            ValueError: When ``review_folder`` is not in ``folders``. Pydantic
                wraps it in a ``ValidationError``; ``_parse`` converts that to
                a ``TaxonomyError`` naming the source file.
        """
        if self.review_folder not in self.folders:
            raise ValueError(
                f"taxonomy declares no uncertainty folder: {self.review_folder!r} is not "
                f"one of its folders ({', '.join(self.folders)}). Every taxonomy needs one "
                "folder that absorbs anything the connector is unsure about, or an uncertain "
                "message has to be forced into a confident answer. Fix it by naming a folder "
                f"{DEFAULT_REVIEW_FOLDER!r}, or by adding a top-level "
                f"{REVIEW_FOLDER_KEY!r} key whose value is one of the folders above."
            )
        return self

    def __contains__(self, folder: object) -> bool:
        """``folder in taxonomy`` -- an exact, case-sensitive membership test.

        The read-only sibling of ``validate_folder``: same exactness, but it
        answers rather than raises or logs. Provided so that a caller merely
        ASKING (a describe path, a test) does not have to catch
        ``UnknownFolderError``, and so an ask cannot be mistaken for the
        invariant-2 rejection in the logs.

        Args:
            folder: Any object, per the ``__contains__`` protocol. A non-string
                is simply absent from the vocabulary.
        """
        return folder in self.folders

    def validate_folder(self, folder: str) -> str:
        """Return ``folder`` if it is in the vocabulary, else raise.

        Named ``validate_folder``, not ``validate``: ``BaseModel.validate`` is
        pydantic's own deprecated v1 constructor, and shadowing it with a
        different signature is a real override conflict mypy refuses. The name
        also matches ``VoiceRouting.validate_voice``, so the two closed
        vocabularies read the same way.

        Matching is EXACT and case-sensitive. A case-insensitive match would
        mean "Review" and "review" address the same folder on one provider and
        two folders on another; exactness keeps provider behaviour identical.
        """
        if folder not in self.folders:
            # GUARD point: the closed vocabulary refusing a folder is the
            # invariant-2 rejection firing, and a burst of these is what a
            # prompt-injection attempt looks like from the outside.
            #
            # The rejected string is TRUNCATED, not logged whole. It is the one
            # value on this path that an attacker can influence -- a hostile
            # email talks the AI into proposing it -- so an unbounded write
            # would let mail content reach the log through the folder argument.
            # The length is what makes the attempt diagnosable; the text is not.
            logger.warning(
                f"TAXONOMY: folder rejected requested={folder[:MAX_LOGGED_FOLDER_CHARS]!r} "
                f"requested_len={len(folder)} allowed_count={len(self.folders)}"
            )
            raise UnknownFolderError(folder, self.folders)
        logger.debug(f"TAXONOMY: folder ok folder={folder} allowed_count={len(self.folders)}")
        return folder

    def describe(self) -> str:
        """Human/AI-readable rendering, for tool docstrings and the digest."""
        return "\n".join(
            f"- {name}: {self.descriptions.get(name, '')}".rstrip() for name in self.folders
        )


def default_taxonomy() -> Taxonomy:
    """The starting vocabulary, before the installer interview replaces it."""
    return Taxonomy(
        folders=tuple(name for name, _ in DEFAULT_FOLDERS),
        descriptions=dict(DEFAULT_FOLDERS),
        review_folder=DEFAULT_REVIEW_FOLDER,
    )


def _first_error_message(exc: ValidationError) -> str:
    """Pull the human sentence out of a pydantic ``ValidationError``.

    ``str(exc)`` renders a multi-line report with the model name, the error
    count, and a docs URL -- correct for a developer, noise for an operator who
    needs one sentence naming the line to change. The validators in this module
    raise ``ValueError`` with that sentence already written, so this returns it
    and falls back to the full render only if the shape is unexpected.
    """
    for error in exc.errors():
        message = str(error.get("msg", ""))
        return message.removeprefix("Value error, ")
    return str(exc)


def _read_folder_entries(
    folders_raw: list[object], source: Path
) -> tuple[list[str], dict[str, str]]:
    """Turn the raw ``folders`` list into ordered names and their descriptions.

    Two entry shapes are accepted -- a bare string, or an object with ``name``
    and an optional ``description`` -- so a minimal taxonomy can be written by
    hand while the installer emits the described form.

    FAILS CLOSED on every defect rather than skipping the bad entry, because
    the vocabulary is the security boundary of invariant 2 and a
    partially-loaded one silently narrows or widens what the connector accepts.

    Args:
        folders_raw: The decoded ``folders`` list, entries unvalidated.
        source: Path it came from, for the error messages.

    Returns:
        The folder names in file order, and the name-to-description map.

    Raises:
        TaxonomyError: On an unrecognised entry shape, an empty name, or a
            duplicate, naming ``source``.
    """
    names: list[str] = []
    descriptions: dict[str, str] = {}
    for entry in folders_raw:
        if isinstance(entry, str):
            name, desc = entry, ""
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            name = entry["name"]
            desc_raw = entry.get("description", "")
            desc = desc_raw if isinstance(desc_raw, str) else ""
        else:
            raise TaxonomyError(f"{source}: each folder must be a string or an object with 'name'.")
        name = name.strip()
        if not name:
            raise TaxonomyError(f"{source}: folder names cannot be empty.")
        if name in descriptions:
            raise TaxonomyError(f"{source}: duplicate folder {name!r}.")
        names.append(name)
        descriptions[name] = desc
    return names, descriptions


def _resolve_review_folder(raw: dict[str, object], source: Path) -> str:
    """Read the declared uncertainty folder, falling back to the name convention.

    Resolution ONLY -- whether the resolved name is actually one of the
    taxonomy's folders is ``Taxonomy``'s own validator to answer, so that a
    taxonomy built in code is held to the same rule as one read from a file.

    The explicit key WINS over the convention when both are present, because a
    file that states the role outright is the more specific instruction and the
    only way to give the role to a folder not called ``Review``.

    Args:
        raw: The decoded top-level JSON object.
        source: Path it came from, for the error message.

    Returns:
        The declared folder name, stripped, or ``DEFAULT_REVIEW_FOLDER``.

    Raises:
        TaxonomyError: When the key is present but not a string.
    """
    declared = raw.get(REVIEW_FOLDER_KEY)
    if declared is None:
        return DEFAULT_REVIEW_FOLDER
    if not isinstance(declared, str):
        raise TaxonomyError(
            f"{source}: {REVIEW_FOLDER_KEY!r} must be a string naming one of the folders."
        )
    return declared.strip()


def _parse(raw: object, source: Path) -> Taxonomy:
    """Turn decoded JSON into a ``Taxonomy``, or raise saying which file is wrong.

    Every rejection names ``source``, because the reader is an operator holding
    a hand-edited install file and "expected a JSON object" without a path is
    unactionable. Two entry shapes are accepted -- a bare string, or an object
    with ``name`` and an optional ``description`` -- so a minimal taxonomy can
    be written by hand while the installer emits the described form.

    Unlike the AI-facing paths, this one FAILS CLOSED on every defect
    (non-list, empty, over ``MAX_FOLDERS``, unnamed, empty name, duplicate, no
    uncertainty folder) rather than skipping the bad entry: the vocabulary is
    the security boundary of invariant 2, and a partially-loaded one silently
    narrows or widens what the connector will accept.

    THE UNCERTAINTY FOLDER IS REQUIRED. It is resolved from an explicit
    top-level ``review_folder`` key when present, else from a folder named
    ``Review``, and a file with neither is refused HERE -- in front of the
    operator holding it -- rather than at the first message the connector
    cannot classify, which is a different day and a different layer.

    Args:
        raw: The decoded JSON value, unvalidated.
        source: Path the value came from, for the error messages.

    Returns:
        The parsed closed vocabulary, folder order preserved.

    Raises:
        TaxonomyError: On any structural or naming defect, including a missing
            or dangling uncertainty folder, naming ``source``.
    """
    if not isinstance(raw, dict):
        raise TaxonomyError(f"{source}: expected a JSON object at the top level.")
    folders_raw = raw.get("folders")
    if not isinstance(folders_raw, list) or not folders_raw:
        raise TaxonomyError(f"{source}: 'folders' must be a non-empty list.")
    if len(folders_raw) > MAX_FOLDERS:
        raise TaxonomyError(
            f"{source}: taxonomy has {len(folders_raw)} folders, limit is {MAX_FOLDERS}."
        )

    names, descriptions = _read_folder_entries(folders_raw, source)

    try:
        return Taxonomy(
            folders=tuple(names),
            descriptions=descriptions,
            review_folder=_resolve_review_folder(raw, source),
        )
    except ValidationError as exc:
        # The model validator already wrote the operator-facing sentence; this
        # only prefixes the file it came from, which is the one fact the model
        # cannot know and the operator needs first.
        raise TaxonomyError(f"{source}: {_first_error_message(exc)}") from exc


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load the installed taxonomy.

    Raises ``TaxonomyError`` when the file is absent. Falling back to the
    default silently would mean a mailbox whose config failed to deploy files
    mail into folders its owner never agreed to, so absence is loud.
    """
    target = path or taxonomy_path()
    if not target.exists():
        raise TaxonomyError(
            f"No taxonomy installed at {target}. "
            "Run 'uv run inbox-zero-connector init-taxonomy' to write the default set, "
            "or install the kit's taxonomy interview output there."
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TaxonomyError(f"{target}: invalid JSON -- {exc}") from exc
    parsed = _parse(raw, target)
    logger.debug(f"TAXONOMY: loaded path={target} folder_count={len(parsed.folders)}")
    return parsed


def write_taxonomy(taxonomy: Taxonomy, path: Path | None = None) -> Path:
    """Write a taxonomy to disk. Installation-time only.

    Not reachable from any MCP tool: the host AI cannot call this, so it cannot
    widen its own vocabulary.
    """
    target = path or taxonomy_path()
    payload = {
        "folders": [
            {"name": name, "description": taxonomy.descriptions.get(name, "")}
            for name in taxonomy.folders
        ],
        # Written ALWAYS, not only when it differs from the convention: a file
        # that states its uncertainty folder outright survives a later rename of
        # the folder in the interview, and reloading what we wrote must produce
        # the taxonomy we had rather than one that re-derives the role by name.
        REVIEW_FOLDER_KEY: taxonomy.review_folder,
    }
    write_text_atomic(target, json.dumps(payload, indent=2) + "\n", what="TAXONOMY")
    logger.info(f"TAXONOMY: written path={target} folder_count={len(taxonomy.folders)}")
    return target
