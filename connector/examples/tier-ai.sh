#!/usr/bin/env bash
# Inbox Zero connector -- AI tier install config (triage + voice + reply drafts).
#
# Writes the connector config for the AI tier: mail is filed into the taxonomy
# folders AND reply drafts are permitted, in the owner's own voice, on the
# provider(s) you enable. Drafts are never sent by this connector; they land in
# the Drafts folder for the owner to review. Microsoft drafts cost no extra
# scope (Mail.ReadWrite already covers them, scope-layer no-send stays intact).
# Gmail is CODE-LAYER either way on the shipped interactive flow: consent
# requests bare gmail.modify with drafts off or on, and that scope is accepted
# by Gmail's send methods, so --google adds capability without adding scope.
# Read README.md "Configure drafts" before passing --google.
#
# Usage:
#   examples/tier-ai.sh --owner-domain example.com [--google] [--force]
#
# --owner-domain  the mailbox owner's own mail domain (internal vs external)
# --google        also enable Gmail drafts (default: Microsoft only)
# --force         overwrite an existing connector-config.json
# --classifier-base-url URL   the OpenAI-compatible endpoint that classifies mail
#                 (THE egress destination; rTech gives the customer the classifier URL)
# --classifier-model ID       model id at that endpoint
# The classifier API KEY is never a flag: paste it into secrets.json (see
# `inbox-zero-connector paths`) after this script runs.
#
# The voice profile is built AFTER sign-in, from the owner's own sent mail,
# inside Claude Desktop (read_sent_sample -> profile the owner approves); see
# ../examples/04-voice-profile-external.md for what that looks like.
set -euo pipefail
cd "$(dirname "$0")/.."

OWNER_DOMAIN=""
FORCE=""
CLASSIFIER_ARGS=()
GOOGLE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner-domain) OWNER_DOMAIN="${2:?--owner-domain needs a value, e.g. example.com}"; shift 2 ;;
    --google) GOOGLE="--enable-google-drafts"; shift ;;
    --force) FORCE="--force"; shift ;;
    --classifier-base-url) CLASSIFIER_ARGS+=(--classifier-base-url "${2:?--classifier-base-url needs a URL}"); shift 2 ;;
    --classifier-model) CLASSIFIER_ARGS+=(--classifier-model "${2:?--classifier-model needs an id}"); shift 2 ;;
    *) echo "Unknown argument: $1 (accepted: --owner-domain, --google, --force, --classifier-base-url, --classifier-model)" >&2; exit 2 ;;
  esac
done
if [[ -z "$OWNER_DOMAIN" ]]; then
  echo "--owner-domain <domain> is required" >&2
  exit 2
fi

echo "== AI tier: file_read_draft, Microsoft drafts on${GOOGLE:+, Google drafts on}"
# shellcheck disable=SC2086
uv run inbox-zero-connector init-config \
  --owner-domain "$OWNER_DOMAIN" \
  --posture file_read_draft \
  --enable-microsoft-drafts \
  $GOOGLE \
  ${CLASSIFIER_ARGS[@]+"${CLASSIFIER_ARGS[@]}"} \
  $FORCE

echo
echo "== Scope manifest for this install (evidence; keep it)"
uv run inbox-zero-connector show-scopes

echo
echo "Next: INSTALL-RUNBOOK.md step 4 (consent to the rTech app) and step 5 (auth)."
if [[ -n "$GOOGLE" ]]; then
  echo "Google drafts are ON. The interactive flow requests no additional scope for"
  echo "this -- bare gmail.modify already covers drafts.create -- so an existing"
  echo "token keeps working and no re-consent is needed. What changed is the"
  echo "guarantee's wording, not the token: see README.md 'The no-send claim'."
fi
