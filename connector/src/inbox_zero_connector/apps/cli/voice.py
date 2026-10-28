"""The ``voice`` subcommand: show, approve, and build a voice profile.

Lives outside ``main.py`` for the same reason ``list_folders.py``,
``show_scopes.py``, and ``desktop.py`` do: that module carries the parser and
dispatch table for every verb and sits against the repo's code-line ceiling,
and this verb group is self-contained -- it reads and writes one JSON store and
shares no state with any other command.

WHY THIS SURFACE EXISTS AT ALL
------------------------------
``services.voice.profile`` refuses to hand an UNAPPROVED profile to drafting,
and the refusal names ``APPROVE_COMMAND``. Until that command existed, the
refusal told the owner to type a verb the package did not have -- so the gate
was real and the way through it was not. These three actions are that way
through, and the store's constant names this module's ``approve`` action.

NOTHING HERE APPROVES ANYTHING BY ITSELF
----------------------------------------
``approve`` is a SEPARATE action rather than a flag on ``build``, matching
``profile.approve`` being a separate function from ``profile.store``. Approval
means the owner READ the profile, and the only evidence of that is their typing
the approve command. A ``build --approve`` would let one invocation both
generate a description of how someone writes and declare that they had read it,
which makes the gate decorative while every test still passes.

``build`` therefore ends by printing the two commands to run next, in order --
read it, then approve it -- rather than by printing "done".

NO MODEL, NO SEND, NO DRAFTING. Nothing here calls a classifier, creates a
draft, or raises the approval flag.

BUILD HAS TWO SOURCES AND THE CALLER PICKS ONE EXPLICITLY
---------------------------------------------------------
``--from-corpus DIR`` reads a directory on disk and constructs no provider at
all. The sent-mail flags (``--sent-label``, ``--sent-after``, ``--sent-before``,
``--sent-limit``) read the EXPLICITLY SELECTED sent sample through the provider,
which is the one place this module touches mail -- ``read_sent_sample`` and
nothing else. Which source runs is decided from the flags the operator gave, as
facts and not preference: no scoring, no blend, no model opinion. Giving both is
refused, and giving neither is refused, because an argument-free ``build`` that
fell back to a mailbox read would be the unbounded sent-folder scrape that
``SentSelection.is_empty`` exists to prevent.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from inbox_zero_connector.config import load_config, load_settings
from inbox_zero_connector.errors import OperatorRefusalError
from inbox_zero_connector.providers import get_provider
from inbox_zero_connector.providers.base import ProviderError, SentSelection
from inbox_zero_connector.services.voice import corpus
from inbox_zero_connector.services.voice import profile as voice_profile
from inbox_zero_connector.services.voice.sentmail import SentMailCorpus


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``voice`` and its three positional actions.

    No flag spellings, unlike ``rules``: there is no shipped demo text naming a
    ``--approve`` form, so the house positional pattern is the only one and
    there is nothing to keep from drifting. ``build`` carries flags because it
    takes OPERANDS -- which source to read -- not because it names an action.
    """
    parser = sub.add_parser(
        "voice",
        help="Show, approve, or build a voice profile.",
        description=(
            "A voice profile describes how the owner WRITES, as patterns and never as "
            "quoted text. It is unusable until the owner reads it and approves it, which "
            "is what 'voice approve' is for; nothing else raises that flag."
        ),
    )
    parser.set_defaults(
        voice_action=None,
        voice=None,
        from_corpus=None,
        sent_label=None,
        sent_after=None,
        sent_before=None,
        sent_limit=None,
    )
    actions = parser.add_subparsers(dest="voice_action")

    show = actions.add_parser("show", help="Print one profile in full, for the owner to read.")
    show.add_argument("voice", help="The voice name, as configured in voice_routing.voices.")

    approve = actions.add_parser("approve", help="Mark a profile read and usable for drafting.")
    approve.add_argument("voice", help="The voice name to approve.")

    _add_build_parser(actions)


def _add_build_parser(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``voice build`` and its two mutually exclusive source spellings.

    Split out of ``add_parser`` to keep that function under the repo's code-line
    ceiling once ``build`` grew a second source. The exclusivity is NOT declared
    to argparse: ``--sent-label`` and friends are four flags that jointly mean
    one source, which ``add_mutually_exclusive_group`` cannot express, and a
    partial declaration would refuse some pairs with argparse's wording and
    others with the command's, from the same rule.
    """
    build = actions.add_parser(
        "build",
        help=(
            "Build a profile from an external corpus directory OR an explicit "
            "sent-mail selection. Stores it UNAPPROVED."
        ),
    )
    build.add_argument("voice", help="The voice name to build.")
    build.add_argument(
        "--from-corpus",
        required=False,
        type=Path,
        dest="from_corpus",
        metavar="DIR",
        help="Directory holding a profile.md describing how the owner writes.",
    )
    build.add_argument(
        "--sent-label",
        action="append",
        dest="sent_label",
        metavar="LABEL",
        help="Sent-mail label to select. Repeatable. Selects the sent-mail source.",
    )
    build.add_argument(
        "--sent-after",
        dest="sent_after",
        metavar="YYYY-MM-DD",
        help="Inclusive lower bound on sent date. Selects the sent-mail source.",
    )
    build.add_argument(
        "--sent-before",
        dest="sent_before",
        metavar="YYYY-MM-DD",
        help="Exclusive upper bound on sent date. Selects the sent-mail source.",
    )
    build.add_argument(
        "--sent-limit",
        type=int,
        dest="sent_limit",
        metavar="N",
        help=(
            "Cap on messages read. NOT a selection on its own -- a limit with no "
            "label or date bound is refused rather than read as 'the whole folder'."
        ),
    )


def format_profile(row: voice_profile.VoiceProfile) -> list[str]:
    """Render one profile as plain text, one labelled line per field.

    Plain text rather than JSON because this is what the OWNER reads before
    approving, and approval is only meaningful if what they read is legible.
    Every field is printed, including the empty ones: a blank ``greeting`` is a
    fact about the corpus the owner should see, and omitting empty rows would
    let a half-built profile look complete.

    List fields get one line per entry for the same reason -- ``never_does``
    joined into a paragraph is a wall of text nobody proofreads, and each entry
    is an independent rule a draft will be held to.
    """
    lines = [
        f"voice:            {row.voice}",
        f"approved:         {'yes' if row.approved else 'no -- not approved'}",
        f"approved_at:      {row.approved_at or '-'}",
        f"built_at:         {row.built_at or '-'}",
        f"greeting:         {row.greeting}",
        f"sign_off:         {row.sign_off}",
        f"sentence_length:  {row.sentence_length}",
        f"formality:        {row.formality}",
        f"structure:        {row.structure}",
    ]
    lines.extend(f"never_does:       {entry}" for entry in row.never_does)
    lines.extend(f"owner_correction: {entry}" for entry in row.owner_corrections)
    lines.append(f"unknowns:         {row.unknowns}")
    lines.append(f"sample_summary:   {row.sample_summary}")
    return lines


def cmd_voice_show(voice: str) -> int:
    """Print one profile in full. Unknown voice is the store's own message."""
    try:
        row = voice_profile.get(voice_profile.load(), voice)
    except voice_profile.ProfileNotFoundError as error:
        logger.trace("voice show: profile not found")
        print(str(error), file=sys.stderr)
        return 1
    for line in format_profile(row):
        print(line)
    return 0


def cmd_voice_approve(voice: str) -> int:
    """Mark one profile read and approved, and save.

    THE ONLY PLACE APPROVAL IS TRIGGERED in this package. The write is asserted
    by reloading from disk in the tests rather than by the printed line: a
    command that printed "approved" without saving would pass a stdout-only
    test and leave drafting still refused.
    """
    profiles = voice_profile.load()
    try:
        updated = voice_profile.approve(profiles, voice, now=datetime.now(UTC))
    except voice_profile.ProfileNotFoundError as error:
        logger.trace("voice approve: profile not found")
        print(str(error), file=sys.stderr)
        return 1
    voice_profile.save(updated)
    row = voice_profile.get(updated, voice)
    print(f"voice profile {row.voice!r} approved at {row.approved_at}")
    return 0


def _sent_selection(args: argparse.Namespace) -> SentSelection:
    """Build the selection from EXACTLY the flags given. Nothing is widened.

    ``--sent-limit`` is passed through only when the operator typed it, so an
    unspecified limit keeps ``SentSelection``'s own default rather than having
    this module invent one. A selection carrying only a limit is left empty on
    purpose: ``is_empty`` is the boundary, and the provider's refusal is the
    message the operator should read.
    """
    fields: dict[str, object] = {
        "labels": tuple(args.sent_label or ()),
        "after": args.sent_after or "",
        "before": args.sent_before or "",
    }
    if args.sent_limit is not None:
        fields["limit"] = args.sent_limit
    return SentSelection(**fields)  # type: ignore[arg-type]


def _sent_corpus(args: argparse.Namespace) -> SentMailCorpus:
    """Construct the provider and the sent-mail corpus over the given selection.

    THE ONLY PLACE THIS MODULE CONSTRUCTS A PROVIDER, and it is reached only
    when a sent flag was given -- the external path never gets here, so building
    from a directory still costs no credential and no network.
    """
    active = load_settings()
    provider = get_provider(
        active.provider,
        drafts_enabled=active.connector_config().drafts_on(active.provider),
    )
    return SentMailCorpus(provider, _sent_selection(args))


def cmd_voice_build(voice: str, args: argparse.Namespace) -> int:
    """Build one profile from the source the operator NAMED, store it UNAPPROVED.

    ``corpus.build_profile`` persists nothing, so the store call here is the
    only write, and it goes through ``profile.store``, which stamps every row
    unapproved on every write. That is what keeps a corpus-built profile landing
    in exactly the same state as one built from sent mail.

    THE SOURCE CHOICE IS THE OPERATOR'S FLAGS, read as facts. Both sources given
    is refused rather than ranked, and neither given is refused rather than
    defaulted -- a bare ``build`` that read the mailbox would be exactly the
    unbounded scrape ``SentSelection`` refuses, arrived at by omission.

    ``ValueError`` catches an unknown voice name (``validate_voice_name``) and
    ``CorpusDocumentError`` -- itself a ``ValueError`` -- catches an unusable
    document, which on the sent path is the thin-sample floor. ``ProviderError``
    is caught ALONGSIDE them because the empty-selection refusal is raised by
    the provider and descends from ``Exception``, not ``ValueError``: without it
    the operator's own "pass labels, a date range, or message_ids" message would
    reach them as a traceback instead of as one stderr line. An
    ``OperatorRefusalError`` (``AuthError`` is both) is re-raised untouched so it
    keeps the CLI's labelled "Authentication error: ..." rendering rather than
    being flattened into an unlabelled line by the wider catch.
    """
    sent_given = any(
        value is not None
        for value in (args.sent_label, args.sent_after, args.sent_before, args.sent_limit)
    )
    directory: Path | None = args.from_corpus

    if sent_given and directory is not None:
        print(
            "voice build takes exactly one source: --from-corpus DIR or a sent-mail "
            "selection (--sent-label/--sent-after/--sent-before/--sent-limit), not both.",
            file=sys.stderr,
        )
        return 1
    if not sent_given and directory is None:
        print(
            "voice build needs a source: --from-corpus DIR, or a sent-mail selection "
            "(--sent-label/--sent-after/--sent-before). There is no default -- the owner "
            "chooses what feeds a voice profile.",
            file=sys.stderr,
        )
        return 1

    if directory is not None:
        source: corpus.CorpusSource = corpus.ExternalCorpusDir(directory)
    else:
        source = _sent_corpus(args)
    try:
        built = corpus.build_profile(
            source,
            voice=voice,
            routing=load_config().require_routing(),
            now=datetime.now(UTC),
        )
    except OperatorRefusalError:
        raise
    except (corpus.CorpusDocumentError, ValueError, ProviderError) as error:
        logger.trace("voice build failed: {}", type(error).__name__)
        print(str(error), file=sys.stderr)
        return 1
    voice_profile.save(voice_profile.store(voice_profile.load(), built, now=datetime.now(UTC)))
    print(f"voice profile {built.voice!r} stored UNAPPROVED. It cannot be used for drafting yet.")
    print(f"  read it:    inbox-zero-connector voice show {built.voice}")
    print(f"  approve it: inbox-zero-connector voice approve {built.voice}")
    return 0


def cmd_voice(
    args: argparse.Namespace, parser_factory: Callable[[], argparse.ArgumentParser]
) -> int:
    """Dispatch one voice-profile operation.

    A bare ``voice`` with no action prints help and exits 1 rather than picking
    a default. ``rules`` defaults to listing because a read is safe there and
    there is only one thing to read; here there is no single voice to show, so
    guessing one would be inventing an argument the operator did not give.

    Args:
        args: The parsed namespace, carrying ``voice_action`` and its operands.
        parser_factory: Builds the full CLI parser, used only to print this
            subcommand's help. Passed in rather than imported because ``main``
            imports THIS module, so reaching back for it would be a cycle.
    """
    actions: dict[str, Callable[[], int]] = {
        "show": lambda: cmd_voice_show(args.voice),
        "approve": lambda: cmd_voice_approve(args.voice),
        "build": lambda: cmd_voice_build(args.voice, args),
    }
    handler = actions.get(args.voice_action or "")
    if handler is None:
        parser_factory().parse_args(["voice", "--help"])
        return 1
    return handler()
