"""Command line entrypoint.

    inbox-zero-connector                    run the MCP stdio server (default)
    inbox-zero-connector serve              run the MCP stdio server
    inbox-zero-connector auth google        interactive OAuth bootstrap
    inbox-zero-connector auth microsoft     interactive OAuth bootstrap
    inbox-zero-connector init-taxonomy      write the default folder taxonomy
    inbox-zero-connector init-config        write draft posture + voice routing
    inbox-zero-connector setup              interactive first-install wizard
    inbox-zero-connector show-scopes        print the scope manifest
    inbox-zero-connector list-folders       print enumerable folders + coverage
    inbox-zero-connector digest [DATE]      print a day's audit digest
    inbox-zero-connector diagnose [--out DIR] write the diagnostics zip
    inbox-zero-connector rules list         list learned filing rules
    inbox-zero-connector rules disable ID   turn one learned rule off
    inbox-zero-connector voice show VOICE   print one voice profile in full
    inbox-zero-connector voice approve VOICE  mark a profile read and usable
    inbox-zero-connector voice build VOICE --from-corpus DIR  build from a corpus
    inbox-zero-connector logout PROVIDER [--yes]  remove a stored credential
    inbox-zero-connector service-unit PROVIDER --platform launchd|systemd
                                            print the supervisor unit for this install
    inbox-zero-connector uninstall PROVIDER [--yes]  guided offboarding

Authentication is a deliberate CLI step. The interactive flow opens a browser
or prints a device code, neither of which works from inside a tool call under
a host AI, so the MCP server never triggers it -- it raises AuthError instead.

Nothing token-like is printed by any command here.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from inbox_zero_connector import __version__
from inbox_zero_connector.apps.cli import (
    credentials,
    desktop,
    diagnose,
    digest,
    init_config,
    list_folders,
    service_unit,
    setup,
    show_scopes,
    status,
    undo,
    uninstall,
    update,
)
from inbox_zero_connector.apps.cli import watch as watch_cli
from inbox_zero_connector.config import (
    ConfigError,
    config_dir,
    config_path,
    data_dir,
    install_id,
    ledger_path,
    load_settings,
    secrets_path,
    taxonomy_path,
)
from inbox_zero_connector.errors import OperatorRefusalError
from inbox_zero_connector.providers import AuthError, get_provider
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.auth import scopes as scopes
from inbox_zero_connector.services.reset import fixture as reset
from inbox_zero_connector.services.triage import learned as learned
from inbox_zero_connector.services.triage.taxonomy import (
    TaxonomyError,
    default_taxonomy,
    write_taxonomy,
)

try:
    from inbox_zero_connector.apps.cli import voice as _voice
except ImportError:  # pragma: no cover -- only the basic tier takes this path.
    _voice = None  # type: ignore[assignment]
    logger.debug("voice verb group absent: basic tier ships without drafting")
"""The ``voice`` verb group, absent from the basic tier.

The basic SKU is sold without drafting: ``scripts/packaging/tiers.json``
withholds ``services/voice/**`` and pins ``posture: file_only``. The verb group
that drives those services is withheld with them, because a command that can
only raise ``ModuleNotFoundError`` is worse than an absent one -- it reads as a
broken install rather than a tier boundary.

A hard import here would make the whole CLI unstartable in that tier, so the
absence is tolerated at exactly one seam and the verb simply does not appear in
``--help`` or the dispatch table. Nothing else in this module branches on it.
"""

if TYPE_CHECKING:
    # DEFERRED TO KEEP THE COLD IMPORT COLD. ``_cmd_watch`` imports the watch
    # loop inside the function body on purpose, so importing it at module scope
    # for a type would undo that (``tests/apps/cli/test_cold_import.py``).
    from inbox_zero_connector.services.watch.loop import PassResult

PROVIDERS = scopes.PROVIDERS

MIN_WATCH_INTERVAL_SECONDS = 1.0
"""Smallest accepted ``watch --interval``. See ``_watch_interval`` for why (#181)."""


def _cmd_init_taxonomy(force: bool) -> int:
    """Write the default folder vocabulary, refusing to clobber without ``force``.

    The refusal is the point rather than politeness: the taxonomy is a CLOSED
    vocabulary the connector files against, so silently replacing an installed
    one would retarget every learned rule and every future filing decision at
    folders the operator did not choose.

    Args:
        force: Overwrite an existing taxonomy file instead of refusing.

    Returns:
        ``0`` when written, ``1`` when a taxonomy already exists and ``force``
        is false.
    """
    target = taxonomy_path()
    if target.exists() and not force:
        print(f"Taxonomy already exists at {target}. Use --force to overwrite.")
        return 1
    taxonomy = default_taxonomy()
    written = write_taxonomy(taxonomy)
    print(f"Wrote default taxonomy to {written}:")
    print(taxonomy.describe())
    print()
    print("This vocabulary is closed: the connector rejects any folder not listed here.")
    return 0


def _cmd_watch(interval_seconds: float, once: bool, dry_run: bool = False) -> int:
    """Build the installed watch context and hand it to the foreground loop.

    ``--dry-run`` IS SCOPED TO ``--once``, AND THE REFUSAL IS THE POINT (#66).
    A continuous preview is a daemon that polls a mailbox forever and files
    nothing: it withholds its cursor every round by design, so it re-reads the
    same mail on every interval, prints the same table, and never converges.
    An operator who left one running would see a watcher that looked healthy
    and an inbox that never cleared, which is the exact failure the connector's
    silent-round warnings exist to catch and this would manufacture on purpose.
    So the combination is refused with a usage error rather than accepted and
    documented against.

    Exit status is the one-shot path's whole product. ``--once`` is what a cron
    entry, an installer smoke test, or a wrapper script calls, and those read
    the status, not the log.

    ONE PASS RAN AND IT WAS CLEAN IS THE ONLY WAY TO EXIT 0 (#160, #175). Four
    things have to be true, and the contract used to check just the first:

    * no stream RAISED (``PassResult.errors``) -- an expired token, a missing
      credential, a provider outage;
    * no message FAILED TO FILE (``RoundSummary.failed``). A stream can
      complete normally and still file nothing: every message it saw takes a
      ``Status.FAILED`` receipt instead of a folder, which leaves ``errors``
      at 0. Reading only the stream-level count means a scheduler never alarms
      on a mailbox where nothing can be filed;
    * a pass actually HAPPENED. ``loop.run`` returns ``None`` when a stop was
      already pending and no poll was made. Nothing was observed, so nothing
      can be reported clean -- exiting 0 there tells cron the mailbox was
      checked and found tidy when it was never read;
    * the pass actually WATCHED SOMETHING. ``PassResult.streams`` is empty
      when the configured scope matched no container -- an ``only_listed``
      list naming a folder that was renamed or misspelled. ``errors`` is 0 and
      there are no summaries, so every other condition above reads clean while
      nothing at all was polled: the same failure as the one before it wearing
      a different shape, and the same answer -- exit 1 and say on stderr which
      setting selected nothing (#175).

    Anything else exits 1. Usage and configuration faults keep the CLI's own
    conventions and do not pass through here.

    The continuous loop is unchanged and still exits 0: surviving errors across
    rounds is its designed behaviour, so its exit status reports only that the
    loop stopped cleanly. That asymmetry is deliberate -- a daemon that dies on
    the first bad round is worse than one that backs off and retries -- so the
    stricter contract above is scoped to ``--once``.
    """
    from inbox_zero_connector.apps.cli import dry_run_preview, watch_accounts
    from inbox_zero_connector.services.watch import loop as watchloop

    if dry_run and not once:
        print(
            "watch --dry-run requires --once. A continuous preview never files "
            "anything and never stores its position, so it would re-read the "
            "same mail every interval and print the same table forever while "
            "the inbox never cleared. Run: inbox-zero-connector watch --once "
            "--dry-run",
            file=sys.stderr,
        )
        return 1
    active = load_settings()
    # ONE ROUND PER CONFIGURED ACCOUNT (#69). An install with no `accounts`
    # list -- every install written before the field existed -- gets exactly
    # one round built from the install-level fields, which is the context this
    # function built inline before the multi-account loop landed.
    rounds, checkpoint = watch_accounts.build_rounds(active, dry_run=dry_run)
    result = watchloop.run(
        rounds,
        interval_seconds=interval_seconds,
        once=once,
        checkpoint=checkpoint,
    )
    if dry_run:
        # THE PREVIEW HAS ITS OWN EXIT CONTRACT, and it is not `--once`'s.
        # `_once_exit_code` returns 0 only for a pass that FILED cleanly, and
        # a dry pass files nothing on purpose -- routed through it, a
        # successful preview of a busy mailbox would still exit 0 by accident
        # of the counts rather than by contract, and a preview of an empty one
        # would exit 1 for having polled a stream that had nothing to show. The
        # question this run answers is "did the preview run", so that is what
        # its status reports; the error conditions `--once` owns still fail
        # first, because they raise or return before this line.
        # THE FIRST ACCOUNT'S PROVIDER, which is the install's default account
        # (`accounts.py`: order is load-bearing) and is the only provider a
        # single-account install has. The preview's folder comparison reads one
        # mailbox's listing; a multi-account preview naming every mailbox's
        # missing folders is a real gap and it is NOT closed here -- widening
        # this call would need a per-account preview table, which is its own
        # change to `dry_run_preview` this lane does not touch.
        return dry_run_preview.exit_code(result, rounds[0].context.provider)
    if once:
        return _once_exit_code(result)
    return 0


def _once_exit_code(result: PassResult | None) -> int:
    """Return 0 only for a pass that RAN, watched a stream, and filed cleanly.

    Separated from ``_cmd_watch`` because it is the whole contract a scheduler
    reads, and it is the part worth testing without building an install. See
    ``_cmd_watch`` for why each of the four conditions is load-bearing
    (#160, #175).

    Args:
        result: What ``loop.run`` returned -- a ``PassResult``, or ``None``
            when a stop landed before any pass ran.

    Returns:
        ``0`` for a clean completed pass, ``1`` otherwise.
    """
    if result is None:
        return 1
    if result.streams_polled == 0:
        print(
            "Watch polled no mail stream: the configured mailbox scope selected "
            "nothing. Check the connector config's mailbox scope for this "
            "provider -- an only_listed scope with a misspelled or renamed "
            "folder matches no container, and nothing is watched. Nothing was "
            "read, so nothing can be reported clean.",
            file=sys.stderr,
        )
        return 1
    if result.errors > 0:
        return 1
    # A stream that raised has `summary is None` and is already counted above;
    # this reads the rounds that COMPLETED and asks whether they could file.
    failed = sum(stream.summary.failed for stream in result.streams if stream.summary is not None)
    return 1 if failed > 0 else 0


def _cmd_reset_snapshot(output: Path, limit: int) -> int:
    """Write one content-free developer placement snapshot."""
    active = load_settings()
    provider_name = active.provider
    connector_config = active.connector_config()
    provider = get_provider(
        provider_name,
        drafts_enabled=connector_config.drafts_on(provider_name),
    )
    try:
        count = reset.write_snapshot(
            provider,
            connector_config.mailbox_scope(provider_name),
            output,
            limit,
        )
    except reset.SnapshotError as exc:
        logger.trace("snapshot failed: {}", type(exc).__name__)
        print(f"Snapshot failed: {exc}")
        return 1
    print(f"Wrote {count} message placement(s) to {output}.")
    return 0


def _cmd_reset_restore(source: Path) -> int:
    """Undo this snapshot's APPLIED ledger writes and list residual findings."""
    active = load_settings()
    provider_name = active.provider
    connector_config = active.connector_config()
    provider = get_provider(
        provider_name,
        drafts_enabled=connector_config.drafts_on(provider_name),
    )
    try:
        report = reset.restore_snapshot(provider, source)
    except (reset.SnapshotError, reset.RestoreError) as exc:
        logger.trace("restore failed: {}", type(exc).__name__)
        print(f"Restore failed: {exc}")
        return 1
    print(
        f"Restore: applied_rows={report.applied_rows} modified={report.modified} "
        f"missing={len(report.missing)} mismatches={len(report.mismatches)}"
    )
    for internet_id in report.missing:
        print(f"Missing: {internet_id}")
    for internet_id in report.mismatches:
        print(f"Finding: {internet_id}")
    # A restore that had rows to undo, undid NONE of them, and still sees
    # placements out of position did not do the thing the operator asked for,
    # and the exit code is what a script and the overnight runbook read. The
    # predicate is deliberately NOT "any mismatch fails": an owner-moved
    # message is a legitimate mismatch on a healthy restore, reported as a
    # Finding with the rest of the run still modified.
    if report.applied_rows > 0 and report.modified == 0 and report.mismatches:
        print(
            f"Restore modified nothing: applied_rows={report.applied_rows} "
            f"modified=0 mismatches={len(report.mismatches)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_reset_cursors(container_id: str | None, all_cursors: bool) -> int:
    """Apply one cursor fixture operation and persist the resulting state."""
    from inbox_zero_connector.services.watch.state import WatchState

    active = load_settings()
    provider_name = active.provider
    state = WatchState.load(provider_name)
    if all_cursors:
        generations = reset.reset_all_cursors(state)
        state.save(provider_name)
        print(f"Reset {len(generations)} cursor(s) for {provider_name}.")
        return 0
    if container_id is None:
        print("Reset requires snapshot, restore, --container, or --all-cursors.")
        return 1
    generation = reset.reset_container_cursor(state, container_id)
    state.save(provider_name)
    print(f"Reset cursor for {container_id}; generation={generation}.")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    """Dispatch one developer-only reset fixture operation."""
    handlers: dict[str | None, Callable[[], int]] = {
        "snapshot": lambda: _cmd_reset_snapshot(args.output, args.limit),
        "restore": lambda: _cmd_reset_restore(args.source),
        None: lambda: _cmd_reset_cursors(args.container, args.all_cursors),
    }
    return handlers[args.reset_action]()


def _format_rule(row: learned.LearnedRule) -> str:
    """One rule as one line. Disabled rows say so first, then why."""
    state = "enabled" if row.enabled else "DISABLED"
    line = (
        f"{row.id:>4}  {state:>8}  {row.predicate.value} {row.value} -> {row.folder}"
        f"  learned_at={row.learned_at}  corrections={row.correction_count}"
    )
    if row.disabled_reason:
        line = f"{line}  reason={row.disabled_reason}"
    return line


def _cmd_rules_list() -> int:
    """Print every row, enabled and disabled alike. Empty is an honest line."""
    rows = learned.load_rules()
    if not rows:
        print("no learned rules")
        return 0
    for row in rows:
        print(_format_rule(row))
    return 0


def _cmd_rules_disable(rule_id: int, reason: str) -> int:
    """Turn one row off and save. Unknown id is the store's own message."""
    rows = learned.load_rules()
    try:
        updated = learned.disable_rule(rows, rule_id, reason)
    except ValueError as error:
        logger.trace("rule disable refused: {}", type(error).__name__)
        print(str(error), file=sys.stderr)
        return 1
    learned.save_rules(updated)
    for row in updated:
        if row.id == rule_id:
            print(_format_rule(row))
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    """Dispatch one learned-rule operation.

    Both spellings land here: the positional ``rules disable 1`` sets
    ``rules_action``, while the demo's ``rules --disable 1`` sets
    ``disable_id``. A bare ``rules`` lists, because a read is the safe default
    for a command with no action.
    """
    rule_id = args.rule_id if args.rules_action == "disable" else args.disable_id
    if rule_id is not None:
        return _cmd_rules_disable(rule_id, args.reason)
    return _cmd_rules_list()


def _cmd_paths() -> int:
    """Print the six resolved on-disk locations by CALLING the config resolvers.

    Every line is the same function the rest of the connector uses, so this
    cannot drift from where files actually land the way a documented path
    would: the locations move with the platform and with any config-dir
    override, and this command is how an operator finds them for backup or
    support without hunting.

    The install id prints FIRST because it is what every line below it is
    namespaced by: when two installs share a machine, the id is the answer to
    "am I looking at the right one", and reading it off the paths themselves
    means guessing which trailing segment is the namespace. A blank value is
    printed rather than omitted, so the un-namespaced case is a stated fact
    instead of a missing line somebody has to interpret.
    """
    print(f"install id: {install_id()}")
    print(f"config dir: {config_dir()}")
    print(f"taxonomy:   {taxonomy_path()}")
    print(f"config:     {config_path()}")
    print(f"secrets:    {secrets_path()}")
    print(f"data dir:   {data_dir()}")
    print(f"ledger:     {ledger_path()}")
    return 0


def _add_install_helper_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the print-only install helpers.

    ``build_parser`` sits against the statement ceiling, so each helper verb
    registers here (or, like ``service-unit``, owns its row in its own module
    per the ``list_folders.add_parser`` convention) rather than inline.
    """
    desktop_parser = sub.add_parser(
        "desktop-config", help="Print the Claude Desktop mcpServers entry for this install."
    )
    desktop_parser.add_argument("provider", choices=["google", "microsoft"])
    service_unit.add_parser(sub)
    uninstall.add_parser(sub)
    # `undo` previews by default and writes to the mailbox only on `--apply`,
    # which is the same posture `reset` has. See `apps/cli/undo.py` for why the
    # apply arm exists now and what still refuses to be reversed.
    undo.add_parser(sub)
    status.add_parser(sub)
    update.add_parser(sub)


def _add_reset_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the developer-only cursor and placement fixtures."""
    parser = sub.add_parser(
        "reset",
        help="DEV/TEST FIXTURE only; not for client mailboxes.",
        description=(
            "DEV/TEST FIXTURE, NOT A SHIPPED FEATURE. Reset watch cursors, or write "
            "and restore a content-free placement snapshot for local tests; never use against "
            "client mailboxes."
        ),
    )
    parser.set_defaults(
        reset_action=None,
        output=None,
        source=None,
        limit=reset.DEFAULT_SNAPSHOT_LIMIT,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--container", help="Reset exactly this container cursor.")
    target.add_argument(
        "--all-cursors",
        action="store_true",
        help="Reset every stored cursor without deleting watch state.",
    )
    actions = parser.add_subparsers(dest="reset_action")
    snapshot = actions.add_parser(
        "snapshot",
        help="Write stable message ids and containers only; never message content.",
    )
    snapshot.add_argument("--out", required=True, type=Path, dest="output")
    snapshot.add_argument(
        "--limit",
        type=int,
        default=reset.DEFAULT_SNAPSHOT_LIMIT,
        help=f"Maximum messages before refusing (default {reset.DEFAULT_SNAPSHOT_LIMIT}).",
    )
    restore = actions.add_parser(
        "restore",
        help="Undo only APPLIED ledger writes after a reset snapshot.",
        description=(
            "DEV/TEST FIXTURE, NOT A SHIPPED FEATURE. Undo only APPLIED ledger writes "
            "after a content-free reset snapshot. Never use against client mailboxes."
        ),
    )
    restore.add_argument(
        "--from",
        required=True,
        type=Path,
        dest="source",
        help="Content-free snapshot written before the test run.",
    )


def _add_rules_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``rules``, which lists and disables learned rules.

    Two spellings, deliberately. The house pattern is a positional action
    subparser (``rules list``), and that is the primary form; the sprint demo
    text says ``rules --list`` / ``rules --disable 1``, so the parent parser
    also carries those flags. Shipping both costs four lines and means the
    demo and the documented form cannot drift apart.

    There is no ``rules add``. Only an observed owner correction writes a row,
    so the CLI reads and disables and never creates.
    """
    parser = sub.add_parser(
        "rules",
        help="List or disable learned filing rules.",
        description=(
            "Learned rules are written by OWNER CORRECTIONS, never by hand and never "
            "by the model. This command reads the table and turns rows off; it cannot "
            "create one."
        ),
    )
    parser.set_defaults(rules_action=None, rule_id=None, disable_id=None, list_flag=False)
    parser.add_argument(
        "--list", action="store_true", dest="list_flag", help="List every rule (same as 'list')."
    )
    parser.add_argument(
        "--disable",
        type=int,
        dest="disable_id",
        metavar="ID",
        help="Disable this rule id (same as 'disable ID').",
    )
    parser.add_argument(
        "--reason", default="disabled by operator", help="Why the rule is being disabled."
    )
    actions = parser.add_subparsers(dest="rules_action")
    actions.add_parser("list", help="Print every rule, enabled and disabled alike.")
    disable = actions.add_parser("disable", help="Turn one rule off, keeping it and its reason.")
    disable.add_argument("rule_id", type=int, help="The rule id to disable.")
    disable.add_argument(
        "--reason", default="disabled by operator", help="Why the rule is being disabled."
    )


def _add_diagnose_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``diagnose``, split out for ``build_parser``'s statement ceiling.

    Same seam every other ``_add_*_parser`` here uses: a subcommand with an
    option of its own gets a function, so the parser builder stays a list of
    rows rather than a script.
    """
    parser = sub.add_parser(
        "diagnose", help="Write a content-free diagnostics zip to send back to rTech."
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="Directory for the zip (default: current directory)."
    )


def _watch_interval(raw: str) -> float:
    """Parse ``--interval`` and refuse anything that is not a real polling delay.

    The floor lives HERE, at the argument boundary, rather than inside the
    watch loop: an operator who typed the wrong number gets a usage error and
    exit 2 before a credential is read, instead of a daemon that appears to
    start (#181). ``argparse`` turns the raised ``ValueError`` into its own
    ``error: argument --interval:`` line, so the message only has to say what
    was wrong with the value.

    Four values got through the bare ``type=float`` this replaces, and each
    breaks the loop differently:

    * ``0`` and negatives poll continuously -- no sleep, so the provider and
      the classifier are hammered with no backoff, which is the reported
      defect;
    * ``nan`` makes every comparison against the deadline false;
    * ``inf`` is a watch that polls once and then never again while still
      reporting itself as running.

    A sub-second interval is refused for the same reason as ``0``: nothing in
    this connector benefits from polling a mailbox faster than once a second,
    and a provider will rate-limit it.

    Args:
        raw: The argument as typed.

    Returns:
        The interval in seconds.

    Raises:
        ValueError: The value is not a finite number at or above the floor.
    """
    value = float(raw)  # A non-numeric string raises ValueError here, as argparse expects.
    if not math.isfinite(value):
        raise ValueError(f"must be a finite number of seconds, not {raw!r}")
    if value < MIN_WATCH_INTERVAL_SECONDS:
        raise ValueError(f"must be at least {MIN_WATCH_INTERVAL_SECONDS:g} seconds, not {value:g}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. One subcommand per row of the dispatch table."""
    parser = argparse.ArgumentParser(
        prog="inbox-zero-connector",
        description=(
            "rTech mailroom MCP connector (Stage 2: triage, voice, reply drafts). "
            "Reads, files, and flags mail, and writes drafts. Cannot send."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the MCP stdio server (default).")
    watch_cli.add_parser(sub, _watch_interval, MIN_WATCH_INTERVAL_SECONDS)
    sub.add_parser("setup", help="Interactive first-install wizard (TTY only).")
    sub.add_parser("show-scopes", help="Print the OAuth scope manifest.")
    list_folders.add_parser(sub)
    sub.add_parser("paths", help="Print config and data locations.")
    _add_install_helper_parsers(sub)

    credentials.add_auth_parser(sub)

    init_parser = sub.add_parser("init-taxonomy", help="Write the default folder taxonomy.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing taxonomy.")

    init_config.add_parser(sub)
    _add_reset_parser(sub)
    _add_rules_parser(sub)
    if _voice is not None:
        _voice.add_parser(sub)

    digest.add_parser(sub)

    _add_diagnose_parser(sub)

    credentials.add_logout_parser(sub)

    return parser


def _dispatch(args: argparse.Namespace, command: str) -> int:
    """Run one subcommand. Dict dispatch, not an if/elif ladder.

    The table is DATA (``_DOCS/STANDARDS-core.md`` ## Control Flow): adding a
    command is one row, the branch count stays at one, and a test can assert
    the command set without invoking any handler.
    """
    handlers: dict[str, Callable[[], int]] = {
        "serve": _cmd_serve,
        "watch": lambda: _cmd_watch(args.interval, args.once, args.dry_run),
        "reset": lambda: _cmd_reset(args),
        "rules": lambda: _cmd_rules(args),
        "auth": lambda: credentials.cmd_auth(args.provider),
        "init-taxonomy": lambda: _cmd_init_taxonomy(args.force),
        "init-config": lambda: init_config.cmd_init_config(args),
        "setup": lambda: setup.cmd_setup(),
        "show-scopes": show_scopes.cmd_show_scopes,
        "list-folders": list_folders.cmd_list_folders,
        "paths": _cmd_paths,
        "desktop-config": lambda: desktop.cmd_desktop_config(args.provider),
        "service-unit": lambda: service_unit.cmd_service_unit(args.provider, args.platform),
        "digest": lambda: digest.cmd_digest(args.day),
        "diagnose": lambda: diagnose.cmd_diagnose(args.out),
        "logout": lambda: credentials.cmd_logout(args.provider, args.yes),
        "uninstall": lambda: uninstall.cmd_uninstall(args.provider, args.yes),
        "undo": lambda: undo.cmd_undo(args.run, args.apply),
        "status": lambda: status.cmd_status(args.interval),
        "update-check": update.cmd_update_check,
    }
    if _voice is not None:
        handlers["voice"] = lambda: _voice.cmd_voice(args, build_parser)
    handler = handlers.get(command)
    if handler is None:
        build_parser().print_help()
        return 1
    return handler()


def _cmd_serve() -> int:
    """Hand off to the MCP server, which owns its own startup sequence."""
    from inbox_zero_connector.apps.server.main import main as serve_main

    serve_main()
    return 0


def _refusal_label(exc: OperatorRefusalError) -> str:
    """Name which kind of refusal this is, for the single stderr line.

    The prefix is the only structure the line has, and it is what an operator
    scans first: "Configuration error" sends them to the config file and
    ``setup``, "Authentication error" sends them to ``auth``, "Taxonomy error"
    to ``init-taxonomy``. Rendering them under one generic word would be a
    regression on the ``ConfigError`` line that already ships, and
    ``Configuration error`` specifically is depended on by the 0.2.10 smoke and
    by the refusal tests, so it stays verbatim.

    An ``isinstance`` chain rather than a class attribute on each exception:
    the label is a presentation concern belonging to the CLI, and putting it on
    the exception would push a rendering decision down into ``config`` and
    ``providers``, which is the coupling those modules are otherwise careful to
    avoid. The fallback is deliberate and is not dead code -- a future refusal
    that lands before anyone thinks about its label still gets a sensible line
    and an exit 1, rather than a traceback.

    Args:
        exc: The refusal about to be rendered.

    Returns:
        The stderr line's prefix, without the separating colon.
    """
    if isinstance(exc, AuthError):
        return "Authentication error"
    if isinstance(exc, TaxonomyError):
        return "Taxonomy error"
    if isinstance(exc, ConfigError):
        return "Configuration error"
    return "Setup error"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, configure logging, and dispatch.

    Logging is configured HERE for every command except ``serve``, which
    configures it itself as part of its startup sequence. Doing it twice would
    build the sinks twice; ``logging_config.setup`` is idempotent, but calling
    it once at the right place is the rule.

    An ``OperatorRefusalError`` is rendered HERE rather than allowed to propagate.
    It is the one exception KIND that always means "the operator's install is
    not set up to do this", never "the connector is broken", and its messages
    are written to be read -- each one already names the field or the command
    that fixes it. An unhandled raise buried that message under a traceback and
    exited through the interpreter, which is non-zero by accident rather than
    by contract; a wrapper script could not tell a refusal from a crash. So it
    becomes one line on stderr and a deliberate ``1``. Every other exception
    still propagates: an unexpected traceback is information, and swallowing it
    would turn a bug report into a shrug (#171).

    THE CATCH IS KEYED ON THE BASE, NOT ON ``ConfigError`` (#195). It caught
    the concrete config class alone, so ``AuthError`` -- the refusal a
    configured-but-unauthenticated install raises, which is the ordinary state
    of a kit between ``setup`` and ``auth`` -- propagated instead, and
    ``watch --once`` on a fresh install printed two provider tracebacks and no
    refusal line. Adding a second ``except`` branch would have fixed that one
    command and left the next sibling refusal to be discovered the same way.

    Args:
        argv: Argument list, defaulting to the process arguments.

    Returns:
        The command's exit status, or ``1`` for an operator refusal.
    """
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    command = args.command or "serve"
    try:
        if command != "serve":
            load_settings()
        return _dispatch(args, command)
    except OperatorRefusalError as exc:
        logger.trace("operator refusal: {}", type(exc).__name__)
        print(f"{_refusal_label(exc)}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
