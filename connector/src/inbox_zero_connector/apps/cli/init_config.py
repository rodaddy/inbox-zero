"""The ``init-config`` subcommand: the flag-driven installer of the security posture.

``setup`` is the same write reached by asking a human at a terminal; this is
the path a script or an installing agent takes (``examples/tier-*.sh`` and
``.ps1`` call exactly this). It owns the most flags in the CLI by design: its
help text is where the scope trade and the egress destination are stated at
the moment somebody is about to make them.

Two flags name what leaves the machine and they are worth reading before
anything else here: ``--enable-*-drafts`` decides the requested OAuth scope,
and ``--classifier-base-url`` decides where subject lines and excerpts are
sent for classification. The classifier API KEY is never a flag -- it lives
only in ``secrets.json`` -- and this module records only that one is required.

The writing itself is delegated to ``write_config``, the single writer the
wizard also uses. Lives outside ``main.py`` for the same reason ``setup.py``
and ``show_scopes.py`` do: that module carries the parser and dispatch table
for every verb and sits against the repo's code-line ceiling.
"""

from __future__ import annotations

import argparse

from inbox_zero_connector.config import (
    DEFAULT_VOICES,
    ClassifierSettings,
    ConnectorConfig,
    Posture,
    VoiceRouting,
    config_path,
    write_config,
)

# From `config_writer` rather than the `config` keystone, which re-exports the
# other two writers. The keystone sits AT its 500-code-line ceiling
# (`scripts/check_code_size.py`), so a third re-export block would breach it --
# and this is the one writer no other module calls, so the indirection buys
# nothing but the breach.
from inbox_zero_connector.config_writer import ensure_install_id
from inbox_zero_connector.services.auth import scopes as scopes

PROVIDERS = scopes.PROVIDERS


def cmd_init_config(args: argparse.Namespace) -> int:
    """Write the installer's decisions. Takes the NAMESPACE, not six positionals.

    Switched from a positional list when the posture flag made it a sixth
    argument and crossed the argument ceiling. Unpacking a namespace into
    positionals only to reassemble it here was never buying anything: this
    subcommand owns the most flags in the CLI by design (its help text is where
    the scope trade is stated), so its argument count grows with every real
    question the installer answers. ``_cmd_reset`` and ``_cmd_rules`` in this
    same module already take the namespace, so this is the file's own pattern
    rather than a new one.

    THE INSTALL ID IS MINTED FIRST, before ``config_path()`` is consulted (#171).
    The id is the last segment of the config directory, so resolving the target
    before minting would test one path for existence and then write to another,
    and the ``--force`` guard would be answering about a directory this install
    no longer uses. Minting first is also what lets ``auth`` build a keychain
    account at all: an install with no id is refused outright, which was the
    fresh-unzip dead end #171 left behind.
    """
    resolved_install_id = ensure_install_id()
    target = config_path()
    if target.exists() and not args.force:
        print(f"Config already exists at {target}. Use --force to overwrite.")
        return 1

    google_drafts: bool = args.enable_google_drafts
    routing = VoiceRouting(
        owner_domain=args.owner_domain.strip().lower().rstrip("."),
        voices=DEFAULT_VOICES,
        sales_labels=tuple(args.sales_labels or ()),
    )
    config = ConnectorConfig(
        drafts_enabled={
            "google": google_drafts,
            "microsoft": args.enable_microsoft_drafts,
        },
        posture=Posture(args.posture),
        routing=routing,
    )
    written = write_config(config, classifier=_classifier_from_args(args))

    print(f"Wrote connector config to {written}.\n")
    print(f"install id   : {resolved_install_id}")
    print(f"posture      : {config.posture.value}")
    print(f"owner domain : {routing.owner_domain}")
    print(f"voices       : {', '.join(routing.voices)}")
    print(f"sales labels : {', '.join(routing.sales_labels) or '(none)'}")
    print()
    for name in PROVIDERS:
        drafts_on = config.drafts_on(name)
        print(f"{name}: drafts={drafts_on}, no-send={scopes.enforcement_for(name, drafts_on)}")
    # Say it HERE too, not only in the log. `load_settings` warns about this
    # state every time the connector starts, but the installer is standing at
    # this prompt right now and will not read a log line they have not caused
    # yet. Naming both fields and the outcome at the moment of the mistake is
    # what keeps "I turned drafting on" from meaning something it does not.
    if config.posture is Posture.FILE_READ_DRAFT:
        for name in PROVIDERS:
            if not config.drafts_on(name):
                print(
                    f"\nNOTE: posture is {Posture.FILE_READ_DRAFT.value} but "
                    f"{name} drafts are OFF, so NO DRAFTS will be composed for "
                    f"{name}. Posture only NARROWS -- it never enables drafts, "
                    f"because the drafts flag is what decides the requested "
                    f"scope. Re-run with --enable-{name}-drafts to enable them."
                )
    if google_drafts:
        print()
        print(f"Google drafts are ON. The interactive token's {scopes.GMAIL_MODIFY} already")
        print("covers draft creation; no re-auth is needed for drafts on Google.")
    return 0


def _classifier_from_args(args: argparse.Namespace) -> ClassifierSettings | None:
    """The vendor switch from the two classifier flags, or None when neither was given.

    None means "leave whatever the file says" (``write_config`` writes the
    block only when handed one), so an install that does not name an endpoint
    keeps the tracked default and no third-party destination is introduced by
    omission. ``requires_key`` follows ``base_url``: a named endpoint is
    assumed to want a key, which is recorded as a FACT about the install, not
    as a value -- the key itself lives only in ``secrets.json``.
    """
    base_url: str | None = args.classifier_base_url or None
    model: str | None = args.classifier_model or None
    if base_url is None and model is None:
        return None
    defaults = ClassifierSettings()
    return ClassifierSettings(
        base_url=base_url,
        model=model or defaults.model,
        requires_key=base_url is not None,
    )


def _add_classifier_flags(parser: argparse.ArgumentParser) -> None:
    """The two vendor-switch flags. Split out for the per-function ceiling.

    They are the ONLY flags here that name an egress destination, so the help
    text says so at the moment the installer is about to type it.
    """
    parser.add_argument(
        "--classifier-base-url",
        default="",
        help=(
            "OpenAI-compatible chat-completions endpoint that classifies mail. THE "
            "EGRESS DESTINATION: subject lines and excerpts are sent there. Omit to "
            "keep the tracked default. The API key is NEVER a flag; it goes in "
            "secrets.json."
        ),
    )
    parser.add_argument(
        "--classifier-model",
        default="",
        help="Model id at that endpoint (default kept if omitted).",
    )


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``init-config``, which owns five of the CLI's flags on its own.

    Split out of ``build_parser`` for the size ceiling, and it is the right
    seam anyway: this is the one subcommand whose flags change the deployment's
    SECURITY POSTURE, so its help text is where the scope trade has to be
    stated at the moment somebody is about to make it.
    """
    parser = sub.add_parser("init-config", help="Write the draft posture and voice routing table.")
    parser.add_argument(
        "--owner-domain", required=True, help="The owner's own mail domain, e.g. example.com."
    )
    parser.add_argument(
        "--posture",
        choices=[member.value for member in Posture],
        default=Posture.FILE_ONLY.value,
        help=(
            "This CLIENT's deployment posture. file_only (the default) files "
            "mail and composes no drafts at all. file_read_draft permits "
            "drafting where the per-provider drafts flag ALSO allows it -- it "
            "never enables drafts on its own, and it does not mark mail read. "
            "Posture can only narrow the drafts flags, never widen them."
        ),
    )
    parser.add_argument(
        "--enable-google-drafts",
        action="store_true",
        help=(
            "Enable Gmail reply drafts. The interactive token's scope already "
            "covers draft creation; this flag is the capability switch the tool "
            "layer honours. Interactive Gmail is code-layer either way."
        ),
    )
    parser.add_argument(
        "--enable-microsoft-drafts",
        action="store_true",
        help=(
            "Enable Graph reply drafts. Costs no extra scope: "
            f"{scopes.GRAPH_MAIL_READWRITE} already covers them, so Microsoft keeps the "
            "scope-layer no-send guarantee."
        ),
    )
    parser.add_argument(
        "--sales-label",
        action="append",
        default=[],
        dest="sales_labels",
        help="Thread label that routes to the sales voice. Repeatable.",
    )
    _add_classifier_flags(parser)
    parser.add_argument("--force", action="store_true", help="Overwrite an existing config.")
