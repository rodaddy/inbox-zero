#!/usr/bin/env bash
# Inbox Zero connector -- BASIC tier install config (folder triage only).
#
# Writes the connector config for the basic tier: mail is filed into the
# taxonomy folders; no reply drafts, no voice profiles, nothing composed and
# nothing sent. On MICROSOFT that is also scope-layer -- Mail.ReadWrite is
# refused by Graph's send paths. On GMAIL it is NOT: the shipped interactive
# consent requests bare gmail.modify, which Gmail's send methods also accept,
# so drafts-off Gmail is a CODE-LAYER guarantee (no send code path exists in
# this connector). Turning drafts off does not change the Gmail scope set --
# see README.md "The no-send claim". Everything else (uv, install, consent to
# the rTech app, sign-in) is INSTALL-RUNBOOK.md; this script is step 3 for the
# basic tier.
#
# Usage:
#   examples/tier-basic.sh --owner-domain example.com [--force]
#
# --owner-domain  the mailbox owner's own mail domain (internal vs external)
# --force         overwrite an existing connector-config.json
# --classifier-base-url URL   the OpenAI-compatible endpoint that classifies mail
#                 (THE egress destination; rTech gives the customer the classifier URL)
# --classifier-model ID       model id at that endpoint
# The classifier API KEY is never a flag: paste it into secrets.json (see
# `inbox-zero-connector paths`) after this script runs.
#
# Same install, same zip as the AI tier: the tier is the config, not the code.
# Switch tiers later by running the other script with --force.
set -euo pipefail
cd "$(dirname "$0")/.."

OWNER_DOMAIN=""
FORCE=""
CLASSIFIER_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner-domain) OWNER_DOMAIN="${2:?--owner-domain needs a value, e.g. example.com}"; shift 2 ;;
    --force) FORCE="--force"; shift ;;
    --classifier-base-url) CLASSIFIER_ARGS+=(--classifier-base-url "${2:?--classifier-base-url needs a URL}"); shift 2 ;;
    --classifier-model) CLASSIFIER_ARGS+=(--classifier-model "${2:?--classifier-model needs an id}"); shift 2 ;;
    *) echo "Unknown argument: $1 (accepted: --owner-domain, --force, --classifier-base-url, --classifier-model)" >&2; exit 2 ;;
  esac
done
if [[ -z "$OWNER_DOMAIN" ]]; then
  echo "--owner-domain <domain> is required" >&2
  exit 2
fi

echo "== BASIC tier: file_only, drafts off on every provider"
# shellcheck disable=SC2086
uv run inbox-zero-connector init-config \
  --owner-domain "$OWNER_DOMAIN" \
  --posture file_only \
  ${CLASSIFIER_ARGS[@]+"${CLASSIFIER_ARGS[@]}"} \
  $FORCE

echo
echo "== Scope manifest for this install (evidence; keep it)"
uv run inbox-zero-connector show-scopes

echo
echo "Next: INSTALL-RUNBOOK.md step 4 (consent to the rTech app) and step 5 (auth)."
