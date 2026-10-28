"""Voice ROUTING: which register a reply is written in, and nothing else.

Split out of the ``config`` keystone for the 500-code-line ceiling, along the
same real seam ``config_paths`` (READS the environment for paths) and
``config_writer`` (WRITES config to disk) were split along. The seam here is
CONFIGURATION versus DECISION: everything in ``config`` answers "what did the
installer set up", while everything here answers "given that setup and this
thread, which voice". This module reads no environment and touches no disk --
it is a pure function of the values ``config`` already loaded, which is why
``CONFIG_MODULES`` in the standards test does not (and must not) list it.

``config`` re-exports ``extract_domain``, ``normalize_address``, and
``resolve_voice`` because src callers import them from the keystone
(``services/triage/learned.py``, ``services/voice/profile.py``,
``services/audit/trace.py``, ``apps/server/main.py``). ``RoutingInputs``,
``VOICE_RULES``, ``VoiceRole``, and ``voice_for_thread`` have no caller outside
this module and its test, so they are imported from HERE -- a re-export exists
to keep a caller working, and one with no caller is surface the keystone pays
for in its line ceiling.

``VoiceRouting`` and ``ConfigError`` are imported from ``config`` at the BOTTOM
of that module, exactly as ``config_writer`` is: this module needs names
``config`` defines, and ``config`` needs the names below, so the import that
closes the cycle goes where every name it wants is already bound.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict

from inbox_zero_connector.config import ConfigError, VoiceRouting

if TYPE_CHECKING:
    from inbox_zero_connector.providers.base import Thread


# ---------------------------------------------------------------------------
# Recipient parsing
# ---------------------------------------------------------------------------


def _bare_address(address: str) -> str:
    """Strip an RFC 5322 display form down to the bare address, lowercased."""
    candidate = address.strip()
    if "<" in candidate and ">" in candidate:
        candidate = candidate[candidate.rfind("<") + 1 : candidate.rfind(">")]
    return candidate.strip().strip("\"'").lower()


def extract_domain(address: str) -> str:
    """Return the lowercased domain of an address, or "" when unparseable.

    Accepts both bare addresses and RFC 5322 display forms
    (``Ann Example <ann@example.com>``). Returning "" rather than raising is
    deliberate: an unparseable recipient must make routing AMBIGUOUS, and
    ambiguous routes to external. Raising here would instead make a malformed
    header abort a draft the owner asked for.
    """
    candidate = _bare_address(address)
    if candidate.count("@") != 1:
        return ""
    domain = candidate.split("@", 1)[1].strip()
    # A trailing dot is legal in a FQDN and must not create a second identity.
    return domain.rstrip(".")


def normalize_address(address: str) -> str:
    """Return the lowercased bare address, or "" when unparseable."""
    candidate = _bare_address(address)
    return candidate if candidate.count("@") == 1 else ""


# ---------------------------------------------------------------------------
# The routing decision -- pure, total, table-driven, and the only place it is
# made.
# ---------------------------------------------------------------------------


class RoutingInputs(BaseModel):
    """What a routing decision is allowed to see. Note what is ABSENT.

    There is no body field, no subject field, and no message text of any kind,
    and that is a signature property rather than a validation rule: a predicate
    in ``VOICE_RULES`` cannot consult content it was never handed, so no
    prompt-injected instruction in an email can reach the voice decision.
    ``tests/test_config_routing.py`` asserts the absence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_domain: str
    recipient_domains: tuple[str, ...]
    """Parsed domains, in order. An unparseable recipient contributes ""."""

    labels: tuple[str, ...]
    sales_labels: frozenset[str]

    def all_internal(self) -> bool:
        """True only when every recipient parsed AND every one is the owner's."""
        return bool(
            self.owner_domain
            and self.recipient_domains
            and all(domain == self.owner_domain for domain in self.recipient_domains)
        )

    def has_sales_label(self) -> bool:
        """True when any thread label is in the installed sales set."""
        return bool(self.sales_labels) and any(
            label.strip().lower() in self.sales_labels for label in self.labels
        )


VoiceRole = Literal["internal_voice", "sales_voice", "default_voice"]

VOICE_RULES: Final[tuple[tuple[Callable[[RoutingInputs], bool], VoiceRole], ...]] = (
    # Rule 1 wins on a genuine all-internal thread even when a sales label is
    # present: the owner is talking to their own colleague about a deal, not to
    # a prospect.
    (RoutingInputs.all_internal, "internal_voice"),
    (RoutingInputs.has_sales_label, "sales_voice"),
    # Total by construction: the last row matches everything, so the loop
    # always returns and there is no fall-through path to reason about.
    (lambda _: True, "default_voice"),
)
"""The routing table. Each row is a predicate and the role it selects.

A table rather than an if/elif ladder (``_DOCS/STANDARDS-core.md`` ## Control
Flow): adding a rule is one row, the branch count stays at one regardless of
how many rules exist, and the table itself can be asserted on in a test without
invoking ``resolve_voice`` at all.
"""


def resolve_voice(
    routing: VoiceRouting,
    recipients: list[str],
    thread_labels: list[str] | None = None,
) -> str:
    """Pick the voice for a reply. PURE: no I/O, no model, no message bodies.

    The AI never chooses. It may not even pass a preference that overrides
    this -- ``create_draft`` accepts a voice argument only so a caller can be
    EXPLICIT about a legal profile, and that argument is validated against the
    installed list, never invented.

    Ambiguity defaults to ``routing.default_voice`` (external). "Ambiguous"
    covers: no recipients at all, every recipient unparseable, and any mix of
    same-domain and other-domain recipients. A thread with one colleague and
    one outsider is EXTERNAL -- treating it as internal would leak the owner's
    unguarded register to the outsider, which is the exact failure this rule
    exists to prevent.

    Args:
        routing: The installed routing table.
        recipients: Addresses the reply would go to. Display forms accepted.
        thread_labels: Provider labels/categories on the thread.

    Returns:
        An installed voice name.

    Raises:
        UnknownVoiceError: If the routing table names a role that is not in its
            own voice list. Unreachable through ``load_config``, which
            validates that at load; reachable through a hand-built
            ``VoiceRouting``.
    """
    inputs = RoutingInputs(
        owner_domain=routing.owner_domain,
        recipient_domains=tuple(extract_domain(item) for item in recipients),
        labels=tuple(thread_labels or ()),
        sales_labels=frozenset(label.lower() for label in routing.sales_labels),
    )
    for matches, role in VOICE_RULES:
        if matches(inputs):
            return routing.validate_voice(getattr(routing, role))
    # Unreachable: the last rule matches everything. Kept as a loud failure
    # rather than an implicit None if somebody edits the table badly.
    raise ConfigError(  # pragma: no cover
        "VOICE_RULES has no terminal rule. ACTION REQUIRED: restore the "
        "always-true final row -- routing must be total."
    )


def voice_for_thread(routing: VoiceRouting, thread: Thread, recipients: list[str]) -> str:
    """Route one thread. Wrapper so call sites cannot pass a body by accident."""
    return resolve_voice(routing, recipients, thread.labels)
