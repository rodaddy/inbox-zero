#!/usr/bin/env python3
"""Build one CUSTOMER-SPECIFIC connector zip, credentials already inside it.

WHY THIS SCRIPT EXISTS
    ``build_zips.py`` produces the SHIPPABLE kit: a generic artifact with
    ``REPLACE-ME`` where every client-specific value belongs, and an install
    runbook telling the customer how to fill them in. That is the right
    artifact for a customer who is doing their own setup, and it is the wrong
    artifact for a managed install, where the whole promise is that the client
    never touches configuration at all. For those, rTech does the setup once
    and hands over a zip that already knows who it is.

    This builder is that second path. It is an rTech-INTERNAL tool and it is
    NEVER shipped: ``tiers.json`` refuses ``scripts/packaging/**`` outright,
    and the ``profiles/**`` entry beside it refuses the customer profiles this
    script reads. Both refusals matter, because a shipped copy of this file
    would tell every customer exactly which vault entries hold other
    customers' credentials.

WHY THE PROFILE HOLDS NO SECRET VALUES
    A profile is a plain JSON file that names WHERE a secret lives -- a vault
    entry name and a field on it -- never the secret itself. A literal value in
    a profile is refused with exit 1 rather than merged, because the profile is
    the artifact most likely to be copied between machines, pasted into a
    ticket, or committed by accident. Nothing that leaks is stored in it, so
    none of those accidents is a credential disclosure.

    The resolution step shells out to the vault CLI and reads ``value`` from
    its JSON. On ANY failure the message names the vault ENTRY and nothing
    else: not the field, not the value, and above all not the subprocess's
    stdout or stderr, which is where a vault tool prints the thing being asked
    for. An error path that echoes the tool's output is the classic way a
    secret reaches a log file that nobody thought of as sensitive.

WHY THE OUTPUT REFUSES TO LAND IN THE REPO
    The zip written here contains live credentials. ``--out`` is resolved and
    rejected if it is the connector root or anywhere beneath it, so the one
    mistake that turns this tool into a credential commit -- writing to
    ``dist/`` out of habit -- fails before a byte is written rather than after
    a ``git add``.

WHAT IT SHARES WITH THE KIT BUILDER
    Member selection, the rules manifest, the connector root, and the fixed
    zip epoch are IMPORTED from ``build_zips`` rather than reimplemented. A
    second copy of the ``never_ship`` walk is a second thing to keep correct,
    and the copy that drifts is the one holding credentials.

Exit codes:
    0  the custom zip was written AND passed its own judge
    1  a real finding -- bad profile contents, a refused --out, a vault miss,
       or an artifact that failed ``assert_zip.py --custom``
    3  harness error; the build could not run and told you nothing

Usage:
    uv run python scripts/packaging/build_custom.py \
        --profile profiles/acme.json --out /secure/handoff/acme
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The sys.path shim above must run before these imports; ruff accepts it.
import build_zips as bz
from assert_zip import SECRETLESS_COMMENT

NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
"""Profile names become a filename component, so they stay boring on purpose."""

DEFAULT_VAULT_CMD = "mcp2cli vaultwarden-secrets get_credential"
"""The fleet's vault reader. Overridable so tests never touch the real vault."""

CONFIG_MEMBER = "config/connector-config.json"
"""The one member replaced with merged bytes instead of the tree's bytes."""

SECRETS_MEMBER = "secrets/config.json"
"""Added by this builder ONLY. ``never_ship`` refuses it from the tree walk."""

MODE_SECRETS = 0o600
"""Stored mode for the resolved credential file: owner read/write, nobody else.

Every other member takes its mode from the git index (0644 or 0755). This one
is not in the index -- it is generated here -- and it is the only member that
holds live credentials, so it gets the narrowest mode the format can express.
It shipped 0644 before #145, meaning the client's unzip left a world-readable
credential file on a machine the connector then ran on unattended.
"""


class BuildError(Exception):
    """A real finding: the operator's inputs are wrong. Exit 1."""


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` onto ``base``, recursing into dicts, override winning.

    Two dicts merge key by key; anything else REPLACES wholesale. A list of
    excluded folders in a profile is meant to be the client's list, not that
    list appended to the default one, and the merge that guesses otherwise
    produces a config no one wrote.
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def split_vault_cmd(vault_cmd: str, posix: bool | None = None) -> list[str]:
    """Split ``--vault-cmd`` into argv the way the HOST shell would.

    ``shlex.split`` defaults to POSIX rules, where a backslash is an escape
    character. On Windows a backslash is the path separator, so POSIX splitting
    silently eats it and turns ``C:\\Python\\python.exe`` into
    ``C:Pythonpython.exe`` -- a path that does not exist, surfacing as a
    ``FileNotFoundError`` from ``subprocess.run`` and reported by the builder as
    an unresolvable vault entry. The cause is nowhere near the message.

    Args:
        vault_cmd: The operator-supplied command string.
        posix: Force a splitting mode. ``None`` picks it from the host, which
            is what production wants; a test passes it explicitly to exercise
            the other platform's rules without needing that platform.

    Returns:
        The argv elements, with separators on Windows left intact.
    """
    if posix is None:
        posix = os.name != "nt"
    return shlex.split(vault_cmd, posix=posix)


def _run_vault(vault_cmd: str, vault: str, field: str) -> str:
    """Resolve one secret, naming only the vault ENTRY if anything goes wrong.

    Args:
        vault_cmd: Shell-ish command string, split with ``split_vault_cmd``.
        vault: The vault entry name to query.
        field: Which field on that entry holds the value.

    Returns:
        The non-empty secret value.

    Raises:
        BuildError: Any failure at all. The message carries the entry name and
            never the subprocess output, which is where the secret itself is.
    """
    # The whole entry, not ``field``: the vault CLI rejects a ``field`` param
    # for this item shape (observed 2026-08-22, exit 4), and the per-field
    # values come back under ``result.fields`` anyway. Selection is local.
    argv = [*split_vault_cmd(vault_cmd), "--params", json.dumps({"query": vault})]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as error:
        raise BuildError(
            f"vault lookup could not run for entry {vault!r}: {type(error).__name__}"
        ) from error
    if result.returncode != 0:
        raise BuildError(f"vault lookup failed for entry {vault!r} (exit {result.returncode})")
    return _value_from_stdout(result.stdout, vault, field)


def _value_from_stdout(stdout: str, vault: str, field: str) -> str:
    """Pull ``result.fields[field]`` out of the vault CLI's JSON without echoing it.

    ``field == "value"`` selects the CLI's top-level ``result.value`` instead,
    which is the item's primary credential. Every failure branch here reports
    only the entry name. The parsed payload holds the credential, so an error
    that quoted the payload to be helpful would put it in whatever captured
    this script's stderr.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise BuildError(f"vault returned non-JSON for entry {vault!r}") from error
    if not isinstance(payload, dict):
        raise BuildError(f"vault returned a non-object payload for entry {vault!r}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise BuildError(f"vault returned no result object for entry {vault!r}")
    fields = result.get("fields")
    value = (
        result.get("value")
        if field == "value"
        else (fields.get(field) if isinstance(fields, dict) else None)
    )
    if not isinstance(value, str) or not value:
        raise BuildError(f"vault returned no usable {field!r} for entry {vault!r}")
    return value


def _resolve_secrets(node: Any, vault_cmd: str) -> Any:
    """Walk the profile's ``secrets`` tree, replacing vault refs with values.

    A leaf is either a ``{"vault": ..., "field": ...}`` reference, which is
    resolved, or something else -- which is refused. Refusing a bare string is
    the whole point: it is what keeps a real credential from being pasted into
    a profile and then travelling wherever the profile travels.
    """
    if isinstance(node, dict):
        if "vault" in node and "field" in node:
            return _run_vault(vault_cmd, str(node["vault"]), str(node["field"]))
        return {key: _resolve_secrets(value, vault_cmd) for key, value in node.items()}
    raise BuildError('secret VALUES never live in a profile -- use {"vault": ..., "field": ...}')


def _validate_profile(profile: dict, rules: dict) -> tuple[str, str]:
    """Return ``(name, zip_tier)`` after refusing anything unusable."""
    name = profile.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise BuildError(f"profile name must match {NAME_PATTERN.pattern}: {name!r}")
    tier = profile.get("zip_tier", "enhanced")
    if tier not in rules["tiers"]:
        raise BuildError(f"unknown zip_tier {tier!r}; known: {sorted(rules['tiers'])}")
    return name, tier


def _validate_out(raw: str) -> Path:
    """Resolve ``--out`` and refuse any path inside the connector tree."""
    out_dir = Path(raw).resolve()
    root = bz.CONNECTOR_ROOT.resolve()
    if out_dir == root or root in out_dir.parents:
        raise BuildError(f"custom zips never land in the repo: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _merged_config(overrides: dict, name: str) -> bytes:
    """The tree's connector config with the profile's overrides merged in.

    ``install_id`` DEFAULTS TO THE PROFILE NAME when the overlay does not set
    one, so every custom zip is isolated without anybody remembering to ask for
    it. Two managed installs on one machine otherwise share a config directory,
    a data directory, and a keychain account, and the second ``auth`` overwrites
    the first mailbox's token -- a failure that looks like the connector filing
    into the wrong inbox rather than like a packaging mistake, which is why the
    safe value is the default rather than a documented option.

    The profile name is already the isolation unit: it is one customer, it is
    the zip filename component, and ``NAME_PATTERN`` is a strict subset of what
    the connector accepts as an install id, so a name that built a zip can
    always be one. An overlay that states ``install_id`` explicitly wins, since
    the merge treats it like any other override.
    """
    with (bz.CONNECTOR_ROOT / CONFIG_MEMBER).open(encoding="utf-8") as handle:
        base = json.load(handle)
    merged = _deep_merge(base, overrides)
    if not merged.get("install_id"):
        merged["install_id"] = name
    return (json.dumps(merged, indent=2) + "\n").encode("utf-8")


def _dirty_shipped(members: list[str]) -> list[str]:
    """Shipped members whose working tree differs from ``HEAD``, sorted.

    Scoped to the member list, so an unrelated dirty file elsewhere in the tree
    is somebody else's lane and not this build's business.

    ``git diff --name-only HEAD --relative`` rather than
    ``git status --porcelain``: porcelain always prints paths from the
    REPOSITORY root and ignores ``status.relativePaths``, and this connector is
    a subdirectory of a larger repo, so those paths would not match the
    arcnames this warning is about. ``--relative`` emits exactly the
    connector-relative names the member list uses.
    """
    try:
        # PATH-resolved `git` with a FIXED argv list and no shell -- the same
        # shape and the same reasoning as `build_zips._tracked_files`: dev
        # machines and CI runners keep git at different absolute paths.
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", "HEAD", "--relative", "--", *members],
            cwd=bz.CONNECTOR_ROOT,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"git diff failed under {bz.CONNECTOR_ROOT}: {error}") from error
    return sorted(name.decode("utf-8") for name in result.stdout.split(b"\0") if name)


def _blob_bytes(arcname: str) -> bytes:
    """The member's content AS COMMITTED, read from ``git show HEAD:<path>``.

    WHY NOT THE WORKING TREE (#145)
        A custom zip is a client handover, and the working tree is whatever the
        packager happened to have open: a half-finished edit, another lane's
        uncommitted change, a debug print. Reading from ``HEAD`` makes the
        artifact a function of a COMMIT, which is the only thing that can be
        named in a handoff note and re-derived later. It also closes the
        credential path -- an uncommitted local edit cannot ride into a client
        zip at all.

        The build does not REFUSE a dirty tree, it uses the blob and WARNS with
        the member list (see ``_build``). Refusing would block a legitimate
        rebuild during active work for no safety gain, since the bytes shipped
        are correct either way; the warning is what tells the packager the zip
        does not contain the edit they are looking at.
    """
    try:
        # PATH-resolved `git`, fixed argv, no shell -- see `_dirty_shipped`.
        result = subprocess.run(
            # `HEAD:./<path>` -- the `./` is load-bearing. `HEAD:<path>` is
            # resolved from the REPOSITORY ROOT regardless of cwd, and this
            # connector is a subdirectory of a larger repo, so the bare form
            # asks for a path that does not exist and git exits 128. The `./`
            # form is the one that resolves relative to cwd.
            ["git", "show", f"HEAD:./{arcname}"],
            cwd=bz.CONNECTOR_ROOT,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"git show HEAD:./{arcname} failed: {error}") from error
    return result.stdout


def _head_files() -> set[str]:
    """Every path present in ``HEAD`` under the connector, arcname form.

    ``build_zips._tracked_files`` answers what the INDEX holds, which is a
    superset of what ``HEAD`` holds: a file that has been ``git add``ed but
    never committed is tracked, so it reaches the member list, and then
    ``git show HEAD:./<path>`` exits 128 because the blob does not exist at
    that commit. The build died with a raw git error naming nothing.

    Same shape and same reasoning as ``_tracked_files``: PATH-resolved ``git``,
    fixed argv, no shell, and a failure is a real finding rather than a
    fallback -- a build that cannot tell what is committed must not guess.
    """
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"],
            cwd=bz.CONNECTOR_ROOT,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"git ls-tree HEAD failed under {bz.CONNECTOR_ROOT}: {error}") from error
    return {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}


def _committed_members(members: list[str]) -> list[str]:
    """``members`` restricted to paths that exist in ``HEAD``, or a named refusal.

    The zip carries committed bytes only (see ``_blob_bytes``), so a member git
    cannot serve from ``HEAD`` is not shippable. Naming the paths is the whole
    fix: the operator needs to know WHICH files to commit, and exit 128 from a
    subprocess told them nothing.
    """
    committed = _head_files()
    uncommitted = sorted(name for name in members if name not in committed)
    if uncommitted:
        raise BuildError(
            "these shipped members are not committed, so HEAD has no bytes for them; "
            f"commit or unstage them first: {', '.join(uncommitted)}"
        )
    return members


def _member_bytes(arcname: str, config_bytes: bytes) -> bytes:
    """Bytes for one member: the merged config, or the COMMITTED file."""
    if arcname == CONFIG_MEMBER:
        return config_bytes
    return _blob_bytes(arcname)


def _write_zip(
    target: Path, members: list[str], config_bytes: bytes, secrets: dict, *, secretless: bool
) -> int:
    """Write the custom archive; return the total member count.

    Same fixed epoch, index-derived modes, and sorted order as the kit builder,
    for the same reason -- two builds of one profile against one commit are
    identical, so "this is the zip we sent" is checkable rather than asserted.
    The modes come from git rather than the working tree so that determinism
    survives; see ``build_zips._tracked_modes``.

    ``SECRETS_MEMBER`` is sorted INTO the member list rather than appended
    after it (#145). Appending made the order a function of which member was
    generated rather than of the member names, which is exactly the property
    the sort exists to remove -- the docstring above claimed sorted order and
    the artifact did not have it.

    ``secretless`` stamps ``SECRETLESS_COMMENT`` into the archive comment. That
    is how the judge tells a profile that legitimately declares no secrets from
    a build whose vault reads all silently returned nothing; see
    ``assert_zip.SECRETLESS_COMMENT``.
    """
    secrets_bytes = (json.dumps(secrets, indent=2) + "\n").encode("utf-8")
    modes = bz._tracked_modes()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        if secretless:
            archive.comment = SECRETLESS_COMMENT
        for arcname in sorted([*members, SECRETS_MEMBER]):
            info = zipfile.ZipInfo(arcname, date_time=bz.FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            # SECRETS_MEMBER is generated, so it is absent from the index; it
            # takes the narrow credential mode rather than the plain default.
            default_mode = MODE_SECRETS if arcname == SECRETS_MEMBER else bz.MODE_PLAIN
            info.external_attr = modes.get(arcname, default_mode) << 16
            data = (
                secrets_bytes if arcname == SECRETS_MEMBER else _member_bytes(arcname, config_bytes)
            )
            archive.writestr(info, data)
    return len(members) + 1


def _sha256(path: Path) -> str:
    """Hex digest of the finished artifact, for the handoff receipt."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the three options this builder takes."""
    parser = argparse.ArgumentParser(description="Build one customer-specific connector zip.")
    parser.add_argument("--profile", required=True, help="path to the customer profile JSON")
    parser.add_argument("--out", required=True, help="output directory, OUTSIDE the repo")
    parser.add_argument("--vault-cmd", default=DEFAULT_VAULT_CMD, help="vault reader command")
    return parser.parse_args(argv)


def _load_inputs(args: argparse.Namespace) -> tuple[dict, dict, str]:
    """Read the profile, the tier rules, and VERSION, or raise for exit 3."""
    with Path(args.profile).open(encoding="utf-8") as handle:
        profile = json.load(handle)
    if not isinstance(profile, dict):
        raise TypeError(f"{args.profile} is not a JSON object")
    rules = bz._load_rules()
    version = (bz.CONNECTOR_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION is empty")
    return profile, rules, version


def _build(profile: dict, rules: dict, version: str, args: argparse.Namespace) -> Path:
    """Do the work once every input has been read. Raises BuildError on exit-1."""
    name, tier = _validate_profile(profile, rules)
    out_dir = _validate_out(args.out)

    # ABSENT or {} means the operator states this install needs no secret --
    # a real case, since the Microsoft public-client path authenticates with a
    # client id that lives in the connector config. Stating it is what lets the
    # judge refuse an EMPTY payload that nobody declared, which is how a build
    # whose vault reads all failed used to pass (#145).
    declared = profile.get("secrets")
    secretless = not declared
    secrets = _resolve_secrets(declared or {}, args.vault_cmd)

    config_bytes = _merged_config(profile.get("config", {}), name)
    # A managed install is the WITHOUT-tests cut (D4, 2026-09-01). The client
    # who receives it never touches configuration, let alone runs the suite,
    # and the draft/voice fixture strings under ``tests/`` are exactly what a
    # client's security reviewer would misread as product behaviour.
    members = _committed_members(bz._members_for(rules, tier, with_tests=False))

    dirty = _dirty_shipped(members)
    if dirty:
        # The artifact carries the COMMITTED bytes; the operator is looking at
        # different ones. Naming them is the difference between a reproducible
        # handoff and a mystery about which build a client actually received.
        print(f"WARNING: {len(dirty)} shipped member(s) differ from HEAD; the zip uses HEAD:")
        for arcname in dirty:
            print(f"  DIRTY (HEAD used) {arcname}")

    target = out_dir / f"inbox-zero-connector-{name}-{version}.zip"
    count = _write_zip(target, members, config_bytes, secrets, secretless=secretless)
    print(f"BUILT {target} members={count} sha256={_sha256(target)}")

    # RUN, not merely printed (#145). ``run_judge`` prints the command first,
    # so the operator still gets a line they can re-run verbatim -- what
    # changed is that an unjudged custom zip can no longer reach a client
    # because whoever built it did not paste the second command. This builder's
    # exit code now means "written AND clean", which is the only claim worth
    # making about an artifact that holds live credentials.
    verdict = bz.run_judge(target, tier, custom=True)
    if verdict != 0:
        raise BuildError(f"the artifact failed its own judge (exit {verdict}): {target}")
    return target


def main(argv: list[str]) -> int:
    """Build one custom zip. See the module docstring for the exit grammar."""
    args = _parse_args(argv)
    try:
        profile, rules, version = _load_inputs(args)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print(f"harness error: {error}", file=sys.stderr)
        return 3
    try:
        _build(profile, rules, version, args)
    except BuildError as error:
        print(f"BUILD FAILED: {error}", file=sys.stderr)
        return 1
    except FileNotFoundError as error:
        print(f"BUILD FAILED: {error}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as error:
        print(f"harness error: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
