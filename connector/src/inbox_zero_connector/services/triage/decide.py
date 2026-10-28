"""THE DECISION LAYER. Which folder, and nothing else.

This module answers exactly one question -- given a message, which taxonomy
folder -- and it performs no mail I/O to do it. There is no provider here, no
folder move, no ledger write, and no network call. A caller takes the
``Classification`` this module returns and decides what to do with it; this
module never acts on its own answer.

That separation is what makes the decision testable. Every input arrives as a
value object, the output is a value object, and the whole module is a pure
function of its arguments -- so a test can drive every branch without a
mailbox, a credential, or a model.

---------------------------------------------------------------------------
1. DETERMINISM FIRST: RULES RUN BEFORE THE MODEL, AND SHORT-CIRCUIT
---------------------------------------------------------------------------

If a script lands the answer 100% of the time, the script owns it. A model is
asked only about the genuinely ambiguous remainder.

THE ORDER IS HAND-WRITTEN RULES, THEN LEARNED RULES, THEN THE MODEL, and each
position is argued rather than incidental:

    1. ``RULES``          hand-written, ships with the kit, encodes INVARIANTS
    2. learned rules      the owner's own corrections, replayed as data
    3. the injected model the genuinely ambiguous remainder

``RULES`` is first because its rows are not filing preferences -- they are
safety properties. "A message with no sender goes to the uncertainty folder"
is true of every mailbox on every install, and it is true precisely BECAUSE
nobody chose it for this mailbox. WHICH folder that is, however, IS the
mailbox's own choice -- the taxonomy declares it (``taxonomy.review_folder``)
and every target in this module resolves through that declaration rather than a
folder name spelled here. A learned row is a habit the owner taught by moving mail, and a
habit must not be able to overrule an invariant: a table that could would let
"this correspondent goes to Vendors" file a message whose origin cannot be
established, which is the one case Review exists for. Ordering settles that
structurally, so no learned row ever has to be validated against the
hand-written table.

Learned rules come second, ahead of the model, for the reason the whole
determinism argument runs on: a learned row is reproducible, auditable, free,
and immune to prompt injection by construction, and every message it settles is
a message no hostile subject line gets to influence. Putting the model first
and the learned table second as a fallback would spend a model call on every
message the owner has ALREADY corrected -- and would answer it
non-deterministically, so the same correction could be needed twice.

The learned table arrives as an ARGUMENT. This module performs no file I/O (see
the opening paragraph), so the caller loads it with ``learned.load_rules`` and
passes it down; ``classify`` stays a pure function of its arguments.

This is not an efficiency preference. A rule is auditable, reproducible, and
free of prompt injection by construction -- ``is_important`` cannot be talked
into anything, where a model reading attacker-controlled subject text can be.
Every message a rule settles is a message no hostile email gets to influence,
so the rule pass is a security control as much as a determinism one.

``RULES`` is DATA -- an ordered tuple of (predicate, folder, reason) rows --
not a chain of ifs, matching ``config.VOICE_RULES``. Adding a rule is one row,
the branch count stays at one however many rules exist, and a test can assert
against the table without invoking the classifier at all.

Order is significance, not convenience: the first matching row wins and the
rest never run.

---------------------------------------------------------------------------
2. WHAT THE RULES CAN ACTUALLY SEE
---------------------------------------------------------------------------

``providers.base.Message`` is a deliberately thin header projection --
``id``, ``thread_id``, ``sender``, ``subject``, ``received``, ``snippet``,
``is_unread``, ``is_important``, ``containers``, ``categories``. It carries NO
raw RFC-5322 headers.

So the textbook bulk-mail rules -- ``List-Unsubscribe`` present implies
Newsletters, ``Precedence: bulk`` implies Newsletters -- are NOT implementable
here FROM HEADERS, and this module does not pretend otherwise. Inferring
"probably bulk"
from subject text would be a guess wearing a rule's clothing: it would carry
``deterministic=True`` and a rule's authority while actually being a heuristic
over attacker-controlled input, which is the worst of both layers. Widening
``Message`` is a provider-boundary change and belongs to whoever owns that
boundary, not to this module.

What remains is the set of facts the provider states about a message rather
than the ones a sender asserts inside it, which is exactly the trustworthy
subset -- and ``categories`` is the richest member of it. Gmail publishes its
OWN bulk-mail verdict there as ``CATEGORY_PROMOTIONS``, so the conclusion the
header rules above were trying to reach arrives pre-computed on that provider,
free, with no extra call.

That asymmetry is expected and is not a defect: the two providers state
different facts, so the deterministic layer covers more of a Gmail mailbox than
a Microsoft one. The rules that exploit it stay inert on Microsoft through the
DATA rather than through a provider branch -- see ``_is_gmail_promotional``.
See ``RULES`` for each row's justification.

---------------------------------------------------------------------------
3. UNCERTAINTY NEVER PRODUCES A GUESS
---------------------------------------------------------------------------

``coerce`` is the hard invariant, and it runs on EVERY result regardless of
which layer produced it. Two conditions send a classification to the
taxonomy's declared uncertainty folder:

    - a folder outside the supplied taxonomy
    - a confidence below ``MIN_CONFIDENCE``

The original suggestion is preserved verbatim in ``reason``, so coercion is
never silent -- the owner reading the ledger sees what was proposed and why it
was refused, and a systematically-coerced folder is a signal the taxonomy or
the classifier needs work.

This is the same instinct as the connector refusing to send. A classifier
forced to answer confidently either buries what mattered or floods the Action
folders with noise, and both teach the owner to stop trusting the system.
The uncertainty folder exists so "I do not know" is a legal answer, and
``Taxonomy`` refuses to load without one so this module always has somewhere to
put it.

---------------------------------------------------------------------------
4. THE MODEL SEAM IS A PROTOCOL, NOT A MODEL
---------------------------------------------------------------------------

``Classifier`` is a structural protocol the caller injects. This module ships
``RuleOnlyClassifier`` -- fully deterministic, no model, no network -- and
wires no LLM. The seam is the deliverable: a real model-backed implementation
lives outside this module and is passed in, so the invariants above hold for
whatever gets injected rather than for one blessed implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol, runtime_checkable

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from inbox_zero_connector.models.mail import Message
from inbox_zero_connector.services.triage import learned as learned
from inbox_zero_connector.services.triage.learned import LearnedMatch, LearnedRule
from inbox_zero_connector.services.triage.taxonomy import Taxonomy

REVIEW_FOLDER: Final = "Review"
"""The conventional name of the uncertainty folder. NOT the coercion target.

Kept as the name the shipped taxonomy uses, but nothing in this module FILES
against it any more. It used to: ``coerce``, ``RuleOnlyClassifier``, and
the no-sender rule each spelled ``"Review"`` here, which meant an installer
interview that renamed the uncertainty folder produced a connector coercing
mail into a folder the mailbox did not have. The decision looked fine and the
filing failed one layer further on, long after the config that caused it.

The coercion target is now ``taxonomy.review_folder`` -- read from the
vocabulary that was actually installed. That does not reintroduce the failure
mode the old wording warned about, because the fallback is not caller-supplied:
``Taxonomy`` refuses at LOAD to exist without an uncertainty folder of its own,
so by the time a taxonomy reaches this module the target is guaranteed present.
"""

MIN_CONFIDENCE: Final = 0.70
"""Below this, a classification is coerced to ``Review``.

0.70 rather than a higher bar because the cost here is ASYMMETRIC and small in
one direction. A coerced message costs the owner one glance at Review; a
confidently misfiled message costs them a missed deadline or a buried invoice,
and they do not find out until it is late. The threshold is set where a
classifier is still meaningfully better than chance across a nine-folder
vocabulary (1/9 ~ 0.11) while leaving no room for a coin-flip to reach a
Business-Action folder.

Not tuned against a labelled corpus -- there is not one yet. It is a stated,
reviewable starting point, and the ledger's coercion records are what a later
adjustment should be argued from.
"""

GMAIL_CATEGORY_PROMOTIONS: Final = "CATEGORY_PROMOTIONS"
"""Gmail's system label for the Promotions tab. A PROVIDER-STATED fact.

Named here rather than inlined because it is a protocol constant of another
system: it is matched by exact id against ``Message.categories``, and a typo in
a bare string literal would fail open -- the rule would simply never fire and
no test that did not already know the correct spelling would notice.

Gmail applies this itself, from its own bulk-mail model. A sender cannot set
it, which is precisely what makes it eligible for a deterministic rule while
``subject`` and ``snippet`` are not.
"""

GMAIL_CATEGORY_UPDATES: Final = "CATEGORY_UPDATES"
"""Gmail's Updates tab. Deliberately NOT routed -- see ``RULES``.

Present as a named constant so the decision not to use it is visible in the
source and testable, rather than being an absence a later reader mistakes for
an oversight.
"""

GMAIL_CATEGORY_FORUMS: Final = "CATEGORY_FORUMS"
"""Gmail's Forums tab. Deliberately NOT routed -- see ``RULES``."""

MAX_LOGGED_REASON_CHARS: Final = 200
"""How much of a reason string reaches the log.

A model-produced reason can quote message text, which is attacker-controlled,
so an unbounded write would let mail content reach the log through the reason
field. Same containment as ``taxonomy.MAX_LOGGED_FOLDER_CHARS``.
"""


class Classification(BaseModel):
    """One filing decision, with its justification and provenance.

    Frozen: a classification is a decision that was made, not a mutable
    accumulator. A caller that wants a different answer produces a new one, so
    the ledger cannot disagree with what was actually applied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    folder: str
    confidence: float = Field(ge=0.0, le=1.0)
    """Bounded at the type, not by a check somebody has to remember to run.

    An out-of-range confidence is a broken classifier, and it must fail at
    construction with the field named rather than silently clearing
    ``MIN_CONFIDENCE`` with a value like 5.0.
    """

    reason: str
    """Short, for the ledger and the owner's audit trail.

    On a coerced result this carries the ORIGINAL suggestion, which is what
    makes a refusal reviewable instead of invisible.
    """

    deterministic: bool
    """True when a rule decided, False when the model did.

    Load-bearing for the audit trail: it is how the owner tells a reproducible
    decision from a judgement call, and how anyone measures what fraction of a
    mailbox the deterministic layer is actually carrying. A model-produced
    result may never claim it -- ``classify`` sets this from which layer ran,
    never from what an implementation reported.
    """


@runtime_checkable
class Classifier(Protocol):
    """The injected decision-maker for what the rules did not settle.

    A Protocol rather than an ABC so an implementation does not have to import
    this module to satisfy it -- the model-backed classifier can live wherever
    its dependencies live, and a test can pass a plain object.

    An implementation is NOT trusted to respect the taxonomy or the confidence
    floor. ``classify`` re-runs ``coerce`` on whatever it returns, because a
    contract stated in a docstring is not a contract a hostile input honours.
    """

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        """Return a folder for ``message``, chosen from ``folders``."""
        ...


class RuleOnlyClassifier:
    """The deterministic fallback: no rule matched, so say so and mean it.

    This is what a deployment with no model configured uses, and it is a
    correct configuration rather than a degraded one. Everything the rules
    cannot settle goes to the taxonomy's uncertainty folder at zero confidence
    -- the owner sorts a pile by hand instead of a machine guessing at it,
    which is the honest trade when there is nothing better to consult.

    Constructed with the installed ``Taxonomy`` because the folder it declines
    into is that taxonomy's to name. The ``Classifier`` protocol passes
    ``folders`` to ``classify``, but a bare tuple of names cannot say WHICH of
    them absorbs doubt, so the answer has to come from the vocabulary object.
    ``taxonomy`` is optional so the zero-argument construction that predates
    this stays valid; omitting it means declining into ``REVIEW_FOLDER``, and
    ``classify`` coerces the result against the real taxonomy either way.
    """

    def __init__(self, taxonomy: Taxonomy | None = None) -> None:
        """Bind the vocabulary whose uncertainty folder this classifier declines into.

        Args:
            taxonomy: The installed closed vocabulary, or ``None`` to fall back
                to the conventional ``Review`` name.
        """
        self._review_folder = taxonomy.review_folder if taxonomy is not None else REVIEW_FOLDER

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        """Decline to guess. Always the uncertainty folder at zero confidence."""
        return Classification(
            folder=self._review_folder,
            confidence=0.0,
            reason="No deterministic rule matched and no model classifier is configured.",
            deterministic=True,
        )


# ---------------------------------------------------------------------------
# The rule table -- data, ordered, first match wins.
# ---------------------------------------------------------------------------


def _is_flagged_important(message: Message) -> bool:
    """True when the PROVIDER marks the message important.

    Trustworthy in a way subject text is not: this reflects the owner's own
    flag or the provider's model of the owner's behaviour, neither of which a
    sender can set from outside. A hostile email cannot mark itself important.
    """
    return message.is_important


def _has_no_sender(message: Message) -> bool:
    """True when the sender is missing or blank.

    A message whose origin cannot be established is exactly what ``Review``
    exists for. Filing it anywhere else means deciding it is safe on the basis
    of the one field that is absent.
    """
    return not message.sender.strip()


def _is_gmail_promotional(message: Message) -> bool:
    """True when GMAIL ITSELF filed the message under Promotions.

    Reads ``categories`` and NOTHING ELSE. Never ``subject``, never ``snippet``:
    those are attacker-controlled text, and a heuristic over hostile input
    carrying this rule's ``deterministic=True`` authority would be worse than
    having no rule at all -- it would launder a guess into the audit trail as a
    reproducible fact. ``categories`` is the provider's own statement ABOUT the
    message, which a sender cannot forge from outside.

    NOT ``containers``, and the distinction is the whole reason the field
    exists. A Gmail tab label describes a message rather than placing it -- a
    promotional message is still in the inbox -- so the adapter reports it as
    an attribute, the way it reports unread. It briefly rode in ``containers``
    because Gmail's wire format spells both as labels, and when the adapter
    started filtering non-places out of ``containers`` this rule lost its input
    and went dead with every test still green.

    INERT ON MICROSOFT BY CONSTRUCTION, with no provider branch anywhere. Graph
    states no category at all, so ``categories`` is ``()`` on every Graph
    message and the membership test cannot match. That is a property of the
    DATA, not of a conditional: there is no ``if provider ==`` here to get out
    of sync with the adapters, and adding one would be strictly worse, because
    a branch is a thing that can be misconfigured while an empty tuple simply
    has nothing to match.
    """
    return GMAIL_CATEGORY_PROMOTIONS in message.categories


RuleRow = tuple[Callable[[Message], bool], str, str]

UNCERTAINTY_FOLDER: Final = "\x00uncertainty"
"""Sentinel folder in ``RULES`` meaning "whatever this taxonomy calls Review".

A row cannot name the uncertainty folder literally, because ``RULES`` is a
module constant built once at import while the folder's name is not known until
a taxonomy is loaded. The alternative -- making ``RULES`` a function of the
taxonomy -- would rebuild the table on every message and lose the property that
the rule set is one inspectable value.

Spelled with a NUL so it can never collide with a real folder name:
``validate_label_name`` and the interview both produce human filing labels, and
a name a mailbox owner could actually type would risk a taxonomy shadowing the
sentinel and silently disabling the substitution.
"""

RULES: Final[tuple[RuleRow, ...]] = (
    (
        _has_no_sender,
        UNCERTAINTY_FOLDER,
        "Sender is missing or blank -- origin cannot be established.",
    ),
    (
        _is_flagged_important,
        "Business-Action",
        "Provider flags this message important; the owner already marked it as needing action.",
    ),
    (
        _is_gmail_promotional,
        "Newsletters",
        "Gmail classified this message as Promotions; the provider states it is bulk mail.",
    ),
)
"""Ordered rows of (predicate, folder, reason). The FIRST match wins.

Short on purpose. Every row here is derivable from provider-stated metadata
alone -- never from ``subject`` or ``snippet``, which are attacker-controlled
text and belong to the model layer where their untrustworthiness is priced in.
A rule that reads sender-supplied content would carry ``deterministic=True``
while actually being a heuristic, and that mislabelling is worse than having
no rule.

The unwritten rows and why they are absent:

    List-Unsubscribe present  -> Newsletters   NOT IMPLEMENTABLE
    Precedence: bulk/list     -> Newsletters   NOT IMPLEMENTABLE
    Auto-Submitted            -> Newsletters   NOT IMPLEMENTABLE

All three want raw RFC-5322 headers, and ``Message`` carries none. They are
the RIGHT rules -- a ``List-Unsubscribe`` header is a genuinely unambiguous
bulk-mail signal, which is why RFC 8058 exists -- and they become one row each
the day ``Message`` grows a headers field. Approximating them from subject text
in the meantime would be a guess with a rule's authority.

WHAT REPLACED THEM ON GMAIL. Those three rows are about reaching a conclusion
Gmail has ALREADY REACHED and already publishes. ``CATEGORY_PROMOTIONS`` is a
system label Gmail applies from its own bulk-mail model, it arrives on the
message's ``labelIds`` at no extra call, and the adapter files it into
``categories`` -- an attribute of the message, not a place it sits. So the
bulk-mail rule is implementable on Gmail TODAY without any header access -- the
header rules above remain the only route for Microsoft, where no equivalent
provider-stated signal exists.

WHY ONLY PROMOTIONS. Gmail ships four category labels and this table routes
exactly one:

    CATEGORY_PROMOTIONS -> Newsletters   ROUTED
    CATEGORY_UPDATES    -> (none)        deliberately unrouted
    CATEGORY_FORUMS     -> (none)        deliberately unrouted
    CATEGORY_SOCIAL     -> (none)        deliberately unrouted

``CATEGORY_UPDATES`` is the load-bearing exclusion. It routinely carries
receipts, order confirmations, and shipping notices -- exactly the mail that
belongs in Receipts -- so routing it to Newsletters would MISFILE a receipt.
The cost is asymmetric in the same direction ``MIN_CONFIDENCE`` is set for: an
unclassified promo costs the owner one glance in Review, while a receipt filed
under Newsletters is a missed invoice they do not discover until it is late.
Leaving a category to the model layer is cheap; misrouting money is not.

``CATEGORY_FORUMS`` and ``CATEGORY_SOCIAL`` are unrouted for a weaker version
of the same reason -- neither maps cleanly onto a single taxonomy folder, and
"probably not important" is a judgement the model layer is allowed to make and
a rule is not. They stay named as constants so the choice is visible rather
than looking like something nobody considered.

WHY NOT ``inferenceClassification``. Microsoft Graph exposes a Focused/Other
split on every message, and it is the obvious candidate for the Graph-side
counterpart to ``CATEGORY_PROMOTIONS`` -- provider-stated, one field, no header
access and no extra call. It stays unrouted, and it is WEAKER than the Gmail
label rather than equivalent. ``Other`` fails for exactly the
``CATEGORY_UPDATES`` reason above: it is Microsoft's not-surfaced bucket, so
receipts, order confirmations, and shipping notices land there as a matter of
course, and routing it to Newsletters would misfile the same money the
exclusion above protects. ``Focused`` fails for a stronger reason -- it is a
PER-USER TRAINED verdict, not a statement about the message. The same mail
reaches Focused in one mailbox and Other in another, and the owner retrains it
every time they move a message between the two tabs. A row reading it would
publish ``deterministic=True`` at confidence 1.0 over a value that is
mailbox-specific by construction, which is the mislabelling this table's first
paragraph exists to forbid. Neither value clears the 100% bar for any folder
this taxonomy routes to, so the field stays out of ``$select`` and out of
``Message`` entirely -- widening the provider boundary to carry a signal no row
may read would buy nothing. Evidence from a real corpus is what would reopen
this, not a better argument.

Sender-domain rules (a known vendor domain implies Receipts) are also absent,
and for a different reason: they need per-mailbox installer data, so they
belong in the config layer's table alongside ``sales_labels``, not hardcoded
here where every buyer would inherit one mailbox's vendors.

TWO ROWS NAME FOLDERS THE INSTALLED TAXONOMY IS FREE TO RENAME OR DROP --
``Business-Action`` and ``Newsletters``. They are shipped-vocabulary names, not
invariants, so ``apply_rules`` SKIPS a row whose folder is absent from the
taxonomy it was handed, logging at DEBUG. Firing it anyway would misfile mail
into a folder that does not exist on a renamed taxonomy, which is precisely the
defect this table's determinism is supposed to rule out. The no-sender row is
different in kind: it targets ``UNCERTAINTY_FOLDER``, which every taxonomy has
by construction, so it can never be skipped.
"""


def apply_rules(message: Message, taxonomy: Taxonomy) -> Classification | None:
    """Run the deterministic pass. Return ``None`` when no row matched.

    ``None`` rather than a default result: "no rule applied" and "a rule chose
    the uncertainty folder" are different facts, and collapsing them would hide
    whether the deterministic layer is doing anything at all.

    Rule results carry confidence 1.0 by definition. A rule that is not certain
    is a heuristic and does not belong in ``RULES``.

    ``taxonomy`` is required, not optional, because a row's target has to be
    resolved against the vocabulary actually installed: ``UNCERTAINTY_FOLDER``
    becomes ``taxonomy.review_folder``, and a row naming a folder this taxonomy
    does not have is SKIPPED rather than fired. Skipping is the conservative
    move -- the message falls through to the learned pass and then the model,
    which is what would have happened had the row never been written, whereas
    firing it would file mail into a folder the mailbox does not have.

    Args:
        message: The message to test each row against.
        taxonomy: The installed closed vocabulary. Supplies the uncertainty
            folder and decides which optional rows are live.

    Returns:
        The first matching row's classification, or ``None``.
    """
    for matches, folder, reason in RULES:
        if not matches(message):
            continue
        target = taxonomy.review_folder if folder == UNCERTAINTY_FOLDER else folder
        if target not in taxonomy:
            # Not a defect and not a warning: a taxonomy is ALLOWED to drop a
            # folder the shipped rule table happens to name. DEBUG records that
            # the row was live and declined, so a "why did this not fire" question
            # has an answer without reading the taxonomy file.
            logger.debug(
                f"CLASSIFY: rule skipped folder={target!r} "
                f"reason='not in installed taxonomy' allowed_count={len(taxonomy.folders)}"
            )
            continue
        return Classification(folder=target, confidence=1.0, reason=reason, deterministic=True)
    return None


def coerce(result: Classification, taxonomy: Taxonomy) -> Classification:
    """Force an illegal or uncertain classification to the uncertainty folder. THE INVARIANT.

    Runs on every result from every layer, including the rules' own -- a rule
    naming a folder this mailbox does not have is a config error, and it must
    surface as a coercion rather than reaching a provider that would then fail
    on an unknown label.

    The original suggestion is preserved in ``reason``, so nothing is refused
    silently and the owner can see what the classifier wanted.

    Args:
        result: What a layer proposed.
        taxonomy: The installed closed vocabulary to check ``folder`` against.

    Returns:
        ``result`` unchanged when it is legal and confident, else a
        ``taxonomy.review_folder`` classification carrying the original
        suggestion.
    """
    review = taxonomy.review_folder
    unknown = result.folder not in taxonomy
    uncertain = result.confidence < MIN_CONFIDENCE
    if not (unknown or uncertain):
        return result

    cause = "outside the installed taxonomy" if unknown else f"below {MIN_CONFIDENCE}"
    # GUARD point: a burst of unknown-folder coercions is what a prompt
    # injection aimed at the classifier looks like from the outside. The
    # proposed folder is truncated for the same reason taxonomy truncates it.
    logger.info(
        f"CLASSIFY: coerced to {review} "
        f"proposed={result.folder[:MAX_LOGGED_REASON_CHARS]!r} "
        f"confidence={result.confidence} cause={cause!r}"
    )
    return Classification(
        folder=review,
        confidence=result.confidence,
        reason=(
            f"Coerced to {review}: proposed folder "
            f"{result.folder[:MAX_LOGGED_REASON_CHARS]!r} is {cause}. "
            f"Original reason: {result.reason[:MAX_LOGGED_REASON_CHARS]}"
        ),
        deterministic=result.deterministic,
    )


def _from_learned(hit: LearnedMatch) -> Classification:
    """Turn a ``LearnedMatch`` into a ``Classification``. THIS module sets the flags.

    ``learned.match`` deliberately returns its own small type rather than a
    ``Classification``, because ``classify`` imports ``learned`` and the reverse
    import would close a cycle. The consequence is the right one anyway:
    ``confidence`` and ``deterministic`` are set HERE, by the layer that knows
    which pass ran, exactly as they are for a ``RULES`` result -- the store
    never gets to describe its own authority.

    Confidence is 1.0 for the same reason ``apply_rules`` uses it: a row either
    matched or it did not. There is no partial match in the predicate
    vocabulary, so a lower number would be a fiction, and a number below
    ``MIN_CONFIDENCE`` would send the owner's own correction to ``Review``.
    """
    return Classification(
        folder=hit.folder,
        confidence=1.0,
        reason=hit.reason,
        deterministic=True,
    )


def classify(
    message: Message,
    taxonomy: Taxonomy,
    classifier: Classifier | None = None,
    *,
    learned_rules: tuple[LearnedRule, ...] | None = None,
) -> Classification:
    """Decide which folder ``message`` belongs in. PURE: no I/O, no mail, no network.

    The order is the policy, and it is the one argued in §1 of the module
    docstring: hand-written ``RULES`` run FIRST and short-circuit, because they
    encode invariants -- a missing sender means the uncertainty folder on every
    install, and a learned habit must not be able to overrule that. The owner's LEARNED rules
    run second, ahead of the model, because they are reproducible and free and
    every message they settle is a message no model call and no hostile subject
    line gets to influence. The injected classifier sees only what neither
    deterministic pass settled, and ``coerce`` runs on whatever comes back from
    all three.

    ``deterministic`` is set HERE from which layer produced the answer, never
    from what an injected implementation claimed. An implementation that
    reports ``deterministic=True`` on a model-produced guess would corrupt the
    audit trail, so its claim is overwritten rather than trusted. A learned
    result is deterministic on the same terms as a ``RULES`` result: a row is
    data evaluated by ordinary code, and replaying it consults nothing.

    Args:
        message: The message to file. Header projection only; no body.
        taxonomy: The installed closed vocabulary. The answer is always one of
            these folders -- when nothing else is certain, its own declared
            ``review_folder``.
        classifier: The injected decision-maker for the ambiguous remainder.
            Defaults to ``RuleOnlyClassifier``, which declines to guess.
        learned_rules: The loaded learned-rule table, or ``None`` when the
            caller has none. Keyword-only, and a LOADED table rather than a
            path: this module performs no file I/O, so the caller reads the
            file with ``learned.load_rules`` and hands the rows down. ``None``
            and ``()`` behave identically -- the pass simply does not fire.

    Returns:
        A legal, sufficiently-confident ``Classification``, or one coerced to
        ``taxonomy.review_folder`` with the original suggestion preserved in
        ``reason``.
    """
    ruled = apply_rules(message, taxonomy)
    if ruled is not None:
        return coerce(ruled, taxonomy)

    learned_hit = learned.match(learned_rules, message) if learned_rules else None
    if learned_hit is not None and learned_hit.folder == taxonomy.review_folder:
        # Uncertainty is a STATE, not a destination. A row naming this
        # taxonomy's uncertainty folder would launder "the connector does not
        # know" into a deterministic, confidence-1.0 answer, which is the one
        # thing the learned table must never be able to say. Skipped rather
        # than refused at load: the watcher loads the table with no taxonomy in
        # hand, so this seam -- which HAS one -- is where the check belongs.
        # Counts only, never the folder name or the sender.
        logger.debug(
            f"LEARNED: skipped row targeting the uncertainty folder rule_id={learned_hit.rule_id}"
        )
        learned_hit = None
    if learned_hit is not None:
        return coerce(_from_learned(learned_hit), taxonomy)

    decider = classifier if classifier is not None else RuleOnlyClassifier(taxonomy)
    proposed = decider.classify(message, taxonomy.folders)
    # The injected layer does not get to describe itself as deterministic. Only
    # RuleOnlyClassifier is, and it is the one implementation this module owns.
    attributed = proposed.model_copy(
        update={"deterministic": isinstance(decider, RuleOnlyClassifier)}
    )
    return coerce(attributed, taxonomy)
