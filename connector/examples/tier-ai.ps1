# Inbox Zero connector -- AI tier install config (triage + voice + reply drafts), Windows.
# macOS/Linux: examples/tier-ai.sh. Same flags, same result.
#
# Writes the connector config for the AI tier: mail is filed into the taxonomy
# folders AND reply drafts are permitted, in the owner's own voice, on the
# provider(s) you enable. Drafts are never sent by this connector; they land in
# the Drafts folder for the owner to review. Microsoft drafts cost no extra
# scope (Mail.ReadWrite already covers them, scope-layer no-send stays intact).
# Gmail is CODE-LAYER either way on the shipped interactive flow: consent
# requests bare gmail.modify with drafts off or on, and that scope is accepted
# by Gmail's send methods, so -Google adds capability without adding scope.
# Read README.md "Configure drafts" before passing -Google.
#
# Usage:
#   .\examples\tier-ai.ps1 -OwnerDomain example.com [-Google] [-Force]
#       [-ClassifierBaseUrl https://.../v1] [-ClassifierModel <id>]
#
# -ClassifierBaseUrl is THE egress destination; rTech gives the customer the
# classifier URL and model id. The classifier API KEY is never a flag: paste it into
# secrets.json (see `uv run inbox-zero-connector paths`) after this runs.
# The voice profile is built AFTER sign-in, from the owner's own sent mail,
# inside Claude Desktop; see ../examples/04-voice-profile-external.md.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OwnerDomain,
    [switch]$Google,
    [switch]$Force,
    [string]$ClassifierBaseUrl = "",
    [string]$ClassifierModel = ""
)
Set-Location -Path (Join-Path $PSScriptRoot "..")

$cliArgs = @("init-config", "--owner-domain", $OwnerDomain, "--posture", "file_read_draft", "--enable-microsoft-drafts")
if ($Google)            { $cliArgs += "--enable-google-drafts" }
if ($ClassifierBaseUrl) { $cliArgs += @("--classifier-base-url", $ClassifierBaseUrl) }
if ($ClassifierModel)   { $cliArgs += @("--classifier-model", $ClassifierModel) }
if ($Force)             { $cliArgs += "--force" }

$label = "AI tier: file_read_draft, Microsoft drafts on"
if ($Google) { $label += ", Google drafts on" }
Write-Host "`n== $label"
& uv run inbox-zero-connector @cliArgs
if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: init-config (exit $LASTEXITCODE)"; exit 1 }

Write-Host "`n== Scope manifest for this install (evidence; keep it)"
& uv run inbox-zero-connector show-scopes
if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: show-scopes (exit $LASTEXITCODE)"; exit 1 }

Write-Host "`nNext: INSTALL-RUNBOOK.md step 4 (consent to the rTech app) and step 5 (auth)."
if ($Google) {
    Write-Host "Google drafts are ON. The interactive flow requests no additional scope for"
    Write-Host "this -- bare gmail.modify already covers drafts.create -- so an existing"
    Write-Host "token keeps working and no re-consent is needed. What changed is the"
    Write-Host "guarantee's wording, not the token: see README.md 'The no-send claim'."
}
