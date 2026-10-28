"""Mail shapes every layer agrees on - Message, Container, and their frozen base.

These are the value objects that cross package boundaries: a provider
constructs them from a wire payload, triage decides on them, the watcher
files them, the server renders them. Until 2026-08-20 they lived in
``providers.base`` and two consumers (``config`` and
``classifiers.openai_compatible``) imported them under ``TYPE_CHECKING``
to dodge the import cycle that created. A root ``models`` package imports
nothing from the rest of the package, so the cycle cannot form.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ProviderModel(BaseModel):
    """Base for every value object crossing the provider boundary.

    Frozen and ``extra="forbid"``: these carry mail out of an adapter, and a
    field nobody declared is a field nobody reviewed. Pydantic rather than a
    bare dataclass so a malformed provider response fails at construction with
    the field named, instead of surfacing later as an ``AttributeError`` in the
    tool layer (``_DOCS/STANDARDS-python.md`` ## LAW: Pydantic for every model).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(ProviderModel):
    """One message header. Deliberately thin.

    Bodies are fetched only through ``get_thread``, so a broad ``list_inbox``
    sweep does not pull entire mailbox contents into the AI's context.
    """

    id: str
    thread_id: str
    sender: str
    subject: str
    received: str
    internet_message_id: str = ""
    snippet: str = ""
    is_unread: bool = False
    is_important: bool = False
    containers: tuple[str, ...] = ()
    """``Container`` ids this message belongs to.

    Graph populates exactly one (the parent folder); Gmail populates all of
    ``labelIds``. A tuple rather than a single id because Gmail's answer is
    genuinely many -- collapsing it would force the adapter to pick one, and
    the adapter has no basis on which to pick.

    Defaults empty so a message constructed without placement information is
    still valid; callers that need placement must handle the empty case rather
    than assuming a container is always present.
    """

    categories: tuple[str, ...] = ()
    """Provider-native category tags that DESCRIBE this message rather than PLACE it.

    Gmail's ``CATEGORY_*`` tab labels are the only current population. They are
    Gmail's own verdict ABOUT a message -- its bulk-mail model saying this is
    promotional, this is an update -- and a tabbed message is still in the
    inbox, so they are never a folder and are never consulted by scope. Kept as
    their own field for exactly the reason ``is_unread`` is: Gmail's wire format
    spells a flag, a category, and a place the same way, and the adapter is the
    one place that conflation gets undone.

    Empty on providers with no such concept. Graph states no category at all,
    so an Outlook message carries ``()`` and any rule reading this field is
    inert there by DATA rather than by a provider branch.
    """


class Container(ProviderModel):
    """One provider-side place a message can be: a Graph folder, a Gmail label.

    Both providers answer the same question -- "where does the provider consider
    this message to be" -- with structurally different objects, so this model is
    the single place that difference is written down instead of being rediscovered
    by each caller.
    """

    id: str
    """Provider-native id. A Graph folder id, or a Gmail ``Label_123``. THE match key.

    Ids rather than names, because a display name is user-editable and localised
    while an id is stable.
    """

    name: str
    """Display name as the provider reports it. For humans and for config matching."""

    well_known: str | None
    """Normalised tag -- "inbox", "junk", "deleted", "archive", "sent" -- or None.

    None means a user-created container with no provider-independent meaning.
    Resolved by the ADAPTER from the provider's own well-known names, never by
    guessing at ``name``: a folder a user called "Junk" is not the junk folder,
    and the provider is the only authority on which one is.
    """

    parent_id: str | None
    """Parent container id, or None at the root or where the provider has no hierarchy.

    Graph mail folders nest and report ``parentFolderId``. Gmail labels do NOT
    expose hierarchy through the API at all -- a label named "Clients/Acme"
    renders as nested in the UI, but the Label resource carries no parent field,
    so the "/" is a display convention and not an API contract. Reading nesting
    out of a Gmail name would be inventing structure the provider never stated.
    """

    exclusive: bool
    """True when a message lives in exactly ONE such container, False when it may hold many.

    This flag is the entire point of this model. It is the documented STRUCTURAL
    DIFFERENCE between the two providers, made explicit here rather than assumed
    by each caller: a Graph message sits in exactly one folder, so moving it out
    of one puts it in another, while a Gmail message routinely carries several
    labels at once and removing one leaves the rest untouched.

    Graph folders are exclusive (True); Gmail labels are not (False). Any caller
    reasoning about placement -- scope checks, filing, watchers -- must branch on
    this rather than assuming the single-container shape that Graph happens to
    have and Gmail does not.
    """
