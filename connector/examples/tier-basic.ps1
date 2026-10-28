# Inbox Zero connector -- BASIC tier install config (folder triage only), Windows.
# macOS/Linux: examples/tier-basic.sh. Same flags, same result.
#
# Writes the connector config for the basic tier: mail is filed into the
# taxonomy folders; no reply drafts, no voice profiles, nothing composed and
# nothing sent. On MICROSOFT that is also scope-layer -- Mail.ReadWrite is
# refused by Graph's send paths. On GMAIL it is NOT: the shipped interactive
# consent requests bare gmail.modify, which Gmail's send methods also accept,
# so drafts-off Gmail is a CODE-LAYER guarantee (no send code path exists in
# this connector). See README.md "The no-send claim".
# Everything else (uv, install, consent to the rTech app, sign-in) is
# INSTALL-RUNBOOK.md; this script is step 3 for the basic tier.
#
# Usage:
#   .\examples\tier-basic.ps1 -OwnerDomain example.com [-Force]
#       [-ClassifierBaseUrl https://.../v1] [-ClassifierModel <id>]
#
# -ClassifierBaseUrl is THE egress destination (subject lines and excerpts are
# sent there); rTech gives the customer the classifier URL and model id. The classifier
# API KEY is never a flag: paste it into secrets.json (see
# `uv run inbox-zero-connector paths`) after this script runs.
#
# ErrorActionPreference stays Continue on purpose (see install.ps1): failure
# detection is an explicit $LASTEXITCODE check.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OwnerDomain,
    [switch]$Force,
    [string]$ClassifierBaseUrl = "",
    [string]$ClassifierModel = ""
)
Set-Location -Path (Join-Path $PSScriptRoot "..")

$cliArgs = @("init-config", "--owner-domain", $OwnerDomain, "--posture", "file_only")
if ($ClassifierBaseUrl) { $cliArgs += @("--classifier-base-url", $ClassifierBaseUrl) }
if ($ClassifierModel)   { $cliArgs += @("--classifier-model", $ClassifierModel) }
if ($Force)             { $cliArgs += "--force" }

Write-Host "`n== BASIC tier: file_only, drafts off on every provider"
& uv run inbox-zero-connector @cliArgs
if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: init-config (exit $LASTEXITCODE)"; exit 1 }

Write-Host "`n== Scope manifest for this install (evidence; keep it)"
& uv run inbox-zero-connector show-scopes
if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: show-scopes (exit $LASTEXITCODE)"; exit 1 }

Write-Host "`nNext: INSTALL-RUNBOOK.md step 4 (consent to the rTech app) and step 5 (auth)."
