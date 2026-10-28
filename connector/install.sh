#!/usr/bin/env bash
# Inbox Zero connector -- deterministic installer (macOS/Linux).
# Windows: use install.ps1. Idempotent: safe to re-run; existing taxonomy and
# config are kept (the CLI's own --force is the only overwrite path).
#
# Usage:
#   ./install.sh --owner-domain example.com   # the install
#   ./install.sh --help                       # usage, exit 0
#   ./install.sh                              # REFUSED, exit 2 -- see
#                                             # require_owner_domain for why
#
# Every run is transcribed to logs/install-<UTC>.log -- that file is the
# install record rTech reads (diagnose ships it whole).
#
# What this script deliberately does NOT do: the identity steps. Microsoft 365
# consent to the shipped rTech app (INSTALL-RUNBOOK.md step 4 -- there is
# nothing to register; bring-your-own is that step's Advanced subsection),
# the sign-in itself (step 5), or enabling drafts (off by default, by design).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
INSTALL_LOG="logs/install-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$INSTALL_LOG") 2>&1
printf 'install transcript: %s\n' "$INSTALL_LOG"

usage() {
  cat <<'EOF'
Usage:
  ./install.sh --owner-domain example.com     install (the domain is REQUIRED)
  ./install.sh --help                         this message

--owner-domain is required because writing the config is what mints this
install's id, and an install with no id cannot build a keychain account.

Windows: use install.ps1 -OwnerDomain example.com
EOF
}

OWNER_DOMAIN=""
case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --owner-domain)
    # Own the diagnostic rather than letting ${2:?} report it: bash names the
    # POSITIONAL INDEX ("line 25: 2:"), and the buyer typed a flag name.
    if [[ -z "${2:-}" ]]; then
      echo "--owner-domain needs a value, e.g. --owner-domain example.com" >&2
      usage >&2
      exit 2
    fi
    OWNER_DOMAIN="$2"
    ;;
  "")
    ;;
  *)
    echo "Unknown argument: $1 (only --owner-domain <domain> is accepted)" >&2
    usage >&2
    exit 2
    ;;
esac

# --owner-domain IS REQUIRED, and the refusal is here rather than at the config
# step so a buyer learns it in a second instead of after `uv sync` (#171).
#
# This used to be optional: a bare `./install.sh` skipped the config step,
# printed a "write one before first use" note, and exited 0. That exit 0 was
# the defect. `init-config` is the surface that MINTS THE INSTALL ID, and the
# id names the config directory, the data directory, and the keychain account.
# An install that never reached it is un-namespaced: `paths` prints an empty
# install id and the first `watch` is refused outright. Since `init-config`
# itself requires --owner-domain, the installer cannot mint one on its own --
# so the only honest options are to demand the domain or to hand back a broken
# install claiming success. It demands the domain.
#
# `setup` (interactive) is the other minting surface and stays available to
# anyone who would rather answer prompts; the usage text names it.
require_owner_domain() {
  [[ -n "$OWNER_DOMAIN" ]] && return 0
  cat >&2 <<'EOF'
--owner-domain is REQUIRED. Without it this installer cannot write a config,
and an install with no config has no install id -- its `paths` shows an empty
install id and its first `watch` is refused, because an un-namespaced keychain
account would collide with any other install on this machine.

  ./install.sh --owner-domain example.com

Prefer to be asked instead of typing flags? Run the interactive wizard, which
mints the same id:

  uv run inbox-zero-connector setup

Nothing was installed. Re-run with the flag above.
EOF
  usage >&2
  exit 2
}

step() { printf '\n== %s\n' "$1"; }

# The zip is built for ONE provider, so the closing instructions name that
# provider's auth verb and no other. Read it from the config the zip ships;
# an unreadable or absent value prints both rather than guessing wrong.
configured_provider() {
  local cfg="config/connector-config.json"
  [[ -r "$cfg" ]] || return 0
  # Plain sed, not a JSON parser: this runs BEFORE `uv sync`, so no Python
  # interpreter of ours is guaranteed present yet, and the only question is
  # which of two literal words the provider field holds.
  local v
  v="$(sed -n 's/.*"provider"[[:space:]]*:[[:space:]]*"\([a-z0-9]*\)".*/\1/p' "$cfg" | head -n 1)"
  case "$v" in google|microsoft) printf '%s' "$v" ;; esac
}

next_steps() {
  local provider auth_lines
  provider="$(configured_provider)"
  case "$provider" in
    google)    auth_lines='  step 5 -- uv run inbox-zero-connector auth google      (browser sign-in)' ;;
    microsoft) auth_lines='  step 5 -- uv run inbox-zero-connector auth microsoft   (browser sign-in)' ;;
    *)         auth_lines='  step 5 -- sign in with the verb for YOUR provider (config/connector-config.json
            did not name one):
              uv run inbox-zero-connector auth google      (browser sign-in)
              uv run inbox-zero-connector auth microsoft   (browser sign-in)' ;;
  esac
  cat <<'EOF'
Remaining steps are identity steps and need a human (INSTALL-RUNBOOK.md):
  step 4 -- consent to the rTech app. Microsoft 365: nothing to register --
            the kit ships rTech's multi-tenant public client, and the sign-in
            prompt in step 5 IS the consent (a tenant that requires admin
            consent needs a Global Admin to approve it once; the runbook has
            the URL). Bringing your own registration is the runbook's
            "Advanced: bring your own registration" subsection, and only if
            your policy demands it -- then record the ids with
            uv run inbox-zero-connector setup (interactive), or the
            init-config flags / env vars (non-interactive; env wins on conflict).
            Secret VALUES are typed by you into the secrets file the wizard
            names -- see uv run inbox-zero-connector paths.
EOF
  printf '%s\n' "$auth_lines"
  cat <<'EOF'
  step 6 -- uv run inbox-zero-connector show-scopes      (the LIVE scope
            manifest -- this is the evidence a security questionnaire wants;
            on a drafts-off install Mail.Send reads as NOT requested)
  step 6b -- one mailbox per install. Installing this same zip twice does not
            give you two installs: both copies resolve the same directories
            and the same keychain account, so the second sign-in overwrites
            the first mailbox's token. A second mailbox needs a second zip.
  step 7 -- uv run inbox-zero-connector watch --once
  step 8 -- uv run inbox-zero-connector desktop-config google|microsoft
            (Claude Desktop JSON; the provider argument is REQUIRED)
  step 9 -- the prove-it checklist
EOF
}

# Print the closing block and stop -- how the installer tests drive it without
# touching uv, the network, or a mailbox.
if [[ "${NEXT_STEPS_ONLY:-}" == "1" ]]; then
  next_steps
  exit 0
fi

require_owner_domain

# uv.lock is lock revision 3, which uv 0.8.0 introduced. An older uv on PATH
# takes the "already installed" branch and then fails `uv sync` with a
# lock-format error that says nothing about upgrading.
UV_MIN_VERSION="0.8.0"

step "uv (installs if missing)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is still not on PATH after the bootstrap. Open a NEW terminal (the" >&2
  echo "installer added \$HOME/.local/bin to PATH for this shell only) and" >&2
  echo "re-run ./install.sh." >&2
  exit 1
fi
uv --version
uv_version="$(uv --version 2>/dev/null | awk '{print $2}')"
# sort -V is the version comparison: if the min sorts first, uv is new enough.
if [[ -z "$uv_version" ]] || [[ "$(printf '%s\n%s\n' "$UV_MIN_VERSION" "$uv_version" | sort -V | head -n 1)" != "$UV_MIN_VERSION" ]]; then
  echo "uv ${uv_version:-<unreadable>} is older than the required $UV_MIN_VERSION" >&2
  echo "(uv.lock is lock revision 3). Upgrade with:  uv self update" >&2
  echo "-- or re-install uv: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

step "dependencies (uv sync -- also fetches Python 3.14 if needed)"
uv sync

# init verbs exit 1 with an "already exists ... Use --force to overwrite."
# notice on re-run; that is the idempotent skip, not a failure. The EXIT CODE
# leads: only exit 1 is eligible, and only then does the exact CLI line decide.
# Any other nonzero -- a uv error, a traceback that merely quotes the phrase --
# is real and stops here.
IDEMPOTENT_SKIP_RE='already exists at .*\. Use --force to overwrite\.'
run_init() {
  local out status
  # `set -e` must not fire on the expected exit 1, hence the guarded capture.
  out=$("$@" 2>&1) && status=0 || status=$?
  if [[ "$status" -eq 0 ]]; then
    printf '%s\n' "$out"
  elif [[ "$status" -eq 1 ]] && printf '%s' "$out" | grep -Eq "$IDEMPOTENT_SKIP_RE"; then
    printf '%s\n' "$out"
  else
    printf '%s\n' "$out" >&2
    printf 'FAILED: %s (exit %s)\n' "$*" "$status" >&2
    exit 1
  fi
}

step "folder taxonomy"
run_init uv run inbox-zero-connector init-taxonomy

# ALWAYS reached: this is the step that MINTS THE INSTALL ID (#171). The id
# names the config dir, the data dir, and the keychain account, and nothing
# else in the installer mints one, so a run that skips this step leaves an
# un-namespaced install whose `paths` prints an empty id and whose first
# `watch` is refused. `init-config` requires --owner-domain, so there is no
# such thing as "run it anyway with no domain"; the OWNER_DOMAIN check above
# is therefore a hard precondition, not a branch.
step "config (drafts stay OFF -- triage-only first posture)"
run_init uv run inbox-zero-connector init-config --owner-domain "$OWNER_DOMAIN"

# The install used to END at a foreground watch the buyer had to remember to
# start (#62). It now ends with the watch handed to the OS supervisor, because
# the failure it prevents is silent: a closed terminal or a reboot stops the
# watch and nothing says so. Idempotent -- launchctl bootout / systemctl
# disable clear any prior registration first, so a re-run replaces rather than
# erroring on a duplicate. A failure here is REPORTED, not fatal: the rest of
# the install is good and the runbook's 7b lines are the manual path.
register_service() {
  local provider unit_path
  provider="$(configured_provider)"
  if [[ -z "$provider" ]]; then
    echo "SKIPPED: config/connector-config.json names no provider, so there is"
    echo "nothing to register. INSTALL-RUNBOOK.md section 7b has the lines."
    return 0
  fi
  case "$(uname -s)" in
    Darwin)
      unit_path="$HOME/Library/LaunchAgents/com.rtech.inbox-zero-connector.$provider.plist"
      mkdir -p "$(dirname "$unit_path")"
      uv run inbox-zero-connector service-unit "$provider" --platform launchd \
        | sed -n '/<?xml/,/<\/plist>/p' > "$unit_path" || return 0
      launchctl bootout "gui/$(id -u)/com.rtech.inbox-zero-connector.$provider" 2>/dev/null || true
      if launchctl bootstrap "gui/$(id -u)" "$unit_path"; then
        echo "REGISTERED: launchd agent com.rtech.inbox-zero-connector.$provider"
        echo "  unit: $unit_path"
        echo "  stop it with: launchctl bootout gui/$(id -u)/com.rtech.inbox-zero-connector.$provider"
        echo "  It will fail-and-retry until step 5 (auth) is done -- that is expected."
      else
        echo "NOT REGISTERED: launchctl bootstrap refused the unit at $unit_path." >&2
        echo "The install is otherwise complete -- see INSTALL-RUNBOOK.md 7b." >&2
      fi
      ;;
    Linux)
      unit_path="$HOME/.config/systemd/user/inbox-zero-connector-$provider.service"
      mkdir -p "$(dirname "$unit_path")"
      uv run inbox-zero-connector service-unit "$provider" --platform systemd \
        | sed -n '/^\[Unit\]/,$p' > "$unit_path" || return 0
      systemctl --user daemon-reload || true
      if systemctl --user enable --now "inbox-zero-connector-$provider.service"; then
        echo "REGISTERED: systemd --user unit inbox-zero-connector-$provider.service"
        echo "  unit: $unit_path"
        echo "  stop it with: systemctl --user disable --now inbox-zero-connector-$provider.service"
        echo "  NOTE: a --user unit stops at logout unless lingering is on"
        echo "        (sudo loginctl enable-linger \$USER) -- see 7b."
        echo "  It will fail-and-retry until step 5 (auth) is done -- that is expected."
      else
        echo "NOT REGISTERED: systemctl refused the unit at $unit_path." >&2
        echo "The install is otherwise complete -- see INSTALL-RUNBOOK.md 7b." >&2
      fi
      ;;
    *)
      echo "SKIPPED: no supervisor known for $(uname -s). INSTALL-RUNBOOK.md 7b."
      ;;
  esac
}

step "verify"
uv run inbox-zero-connector --version
uv run inbox-zero-connector paths
uv run inbox-zero-connector show-scopes

step "background service"
register_service

step "machine setup complete"
next_steps
