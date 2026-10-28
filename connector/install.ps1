# Inbox Zero connector -- deterministic installer (Windows PowerShell).
# macOS/Linux: use install.sh. Idempotent: safe to re-run; existing taxonomy
# and config are kept (the CLI's own --force is the only overwrite path).
#
# Usage:
#   .\install.ps1 -OwnerDomain example.com     # the install
#   .\install.ps1                              # REFUSED, exit 2 -- writing the
#                                              # config is what mints the
#                                              # install id, so it is required
#
# If Windows refuses to run it ("not digitally signed" / "cannot be loaded"),
# the file arrived from the internet and is blocked. Run this once, from the
# folder holding the script, then re-run the line above:
#   Unblock-File .\install.ps1; Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# -Scope Process lasts for that window only and changes nothing machine-wide.
#
# Every run is transcribed to logs\install-<UTC>.log (the install record rTech
# reads; diagnose ships it whole).
#
# What this script deliberately does NOT do: the identity steps. Microsoft 365
# consent to the shipped rTech app (INSTALL-RUNBOOK.md step 4 -- there is
# nothing to register; bring-your-own is that step's Advanced subsection),
# the sign-in itself (step 5), or enabling drafts (off by default, by design).
#
# ErrorActionPreference stays Continue on purpose: the connector logs to
# stderr, and under Windows PowerShell 5.1 a Stop preference turns any
# redirected stderr line from a native command into a terminating error
# (proven in CI run 32496124472). Failure detection is explicit
# $LASTEXITCODE checks instead.
[CmdletBinding()]
param([string]$OwnerDomain = "")
Set-Location -Path $PSScriptRoot

New-Item -ItemType Directory -Force -Path "logs" | Out-Null
# InvariantCulture pinned: under a non-en-US culture the custom format string
# can render differently, and rTech's "send me logs\install-<UTC>.log" glob
# then does not match what the client actually has.
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'", [Globalization.CultureInfo]::InvariantCulture)
$installLog = Join-Path "logs" ("install-" + $stamp + ".log")
Start-Transcript -Path $installLog -Append | Out-Null
Write-Host "install transcript: $installLog"

function Step([string]$Name) { Write-Host "`n== $Name" }

function Assert-Ok([string]$What) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $What (exit $LASTEXITCODE)"
        exit 1
    }
}

# init verbs exit 1 with an "already exists ... Use --force to overwrite."
# notice on re-run; that is the idempotent skip, not a failure. The EXIT CODE
# leads: only exit 1 is eligible, and only then does the exact CLI line decide.
# Any other nonzero -- a uv error, a traceback that merely quotes the phrase --
# is real and stops here.
#
# $LASTEXITCODE is captured on the line immediately after the pipeline, before
# anything else can overwrite it, and a CommandNotFoundException from `uv` is
# caught here rather than leaving a stale code in place (uv resolvability is
# already asserted before this ever runs).
function Run-Init([string[]]$CliArgs) {
    try {
        $out = (& uv run inbox-zero-connector @CliArgs 2>&1 | ForEach-Object { "$_" }) -join "`n"
        $exit = $LASTEXITCODE
    } catch {
        Write-Host "FAILED: inbox-zero-connector $($CliArgs -join ' ') -- $($_.Exception.Message)"
        exit 1
    }
    Write-Host $out
    $idempotentSkip = ($exit -eq 1) -and ($out -match 'already exists at .*\. Use --force to overwrite\.')
    if ($exit -ne 0 -and -not $idempotentSkip) {
        Write-Host "FAILED: inbox-zero-connector $($CliArgs -join ' ') (exit $exit)"
        exit 1
    }
}

# The zip is built for ONE provider, so the closing instructions name that
# provider's auth verb and no other. Read it from the config the zip ships;
# an unreadable or absent value prints both rather than guessing wrong.
function Get-ConfiguredProvider {
    $cfg = Join-Path "config" "connector-config.json"
    if (-not (Test-Path $cfg)) { return "" }
    try {
        $provider = (Get-Content -Raw $cfg | ConvertFrom-Json).provider
    } catch {
        return ""
    }
    if ($provider -eq "google" -or $provider -eq "microsoft") { return $provider }
    return ""
}

# The install used to END at a foreground watch the buyer had to remember to
# start (#62). It now hands the watch to Task Scheduler, because the failure it
# prevents is silent: a closed window or a reboot stops the watch and nothing
# says so. Idempotent -- the emitted script uses Register-ScheduledTask -Force,
# which REPLACES a task of the same name instead of erroring on a duplicate.
# A failure here is REPORTED, not fatal: the rest of the install is good and
# INSTALL-RUNBOOK.md section 7b is the manual path.
function Register-Service {
    $provider = Get-ConfiguredProvider
    if ($provider -eq "") {
        Write-Host "SKIPPED: config\connector-config.json names no provider, so there is"
        Write-Host "nothing to register. INSTALL-RUNBOOK.md section 7b has the lines."
        return
    }
    $taskName = "com.rtech.inbox-zero-connector.$provider"
    try {
        $script = uv run inbox-zero-connector service-unit $provider --platform schtasks
        if ($LASTEXITCODE -ne 0) { throw "service-unit exited $LASTEXITCODE" }
        # The command prints guidance around the script; the PowerShell lines
        # are the ones that assign or register, so take exactly those.
        $lines = $script -split "`r?`n" | Where-Object {
            $_ -match '^\$(action|trigger|settings|principal) =' -or
            $_ -match '^(Register-ScheduledTask|\[Environment\])'
        }
        if (-not $lines) { throw "service-unit printed no registration lines" }
        Invoke-Expression ($lines -join "`n") | Out-Null
    } catch {
        Write-Host "NOT REGISTERED: $($_.Exception.Message)"
        Write-Host "The install is otherwise complete -- see INSTALL-RUNBOOK.md 7b."
        return
    }
    Write-Host "REGISTERED: scheduled task $taskName (at logon, restarts on failure)"
    Write-Host "  confirm with: Get-ScheduledTask -TaskName '$taskName'"
    Write-Host "  stop it with: Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
    Write-Host "  It will fail-and-retry until step 5 (auth) is done -- that is expected."
}

function Write-NextSteps {
    $provider = Get-ConfiguredProvider
    switch ($provider) {
        "google"    { $authLines = "  step 5 -- uv run inbox-zero-connector auth google      (browser sign-in)" }
        "microsoft" { $authLines = "  step 5 -- uv run inbox-zero-connector auth microsoft   (browser sign-in)" }
        default     { $authLines = @"
  step 5 -- sign in with the verb for YOUR provider (config\connector-config.json
            did not name one):
              uv run inbox-zero-connector auth google      (browser sign-in)
              uv run inbox-zero-connector auth microsoft   (browser sign-in)
"@ }
    }
    Write-Host @"
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
"@
    Write-Host $authLines
    Write-Host @"
  step 6 -- uv run inbox-zero-connector show-scopes      (the LIVE scope
            manifest -- this is the evidence a security questionnaire wants;
            on a drafts-off install Mail.Send reads as NOT requested)
  step 6b -- one mailbox per install. Installing this same zip twice does not
            give you two installs: both copies resolve the same directories
            and the same keychain account, so the second sign-in overwrites
            the first mailbox's token. A second mailbox needs a second zip.
  step 7 -- uv run inbox-zero-connector watch --once
  step 8 -- uv run inbox-zero-connector desktop-config   (Claude Desktop JSON)
  step 9 -- the prove-it checklist
"@
}

try {

# -OwnerDomain IS REQUIRED, and the refusal is here rather than at the config
# step so a buyer learns it in a second instead of after `uv sync` (#171).
# install.sh's require_owner_domain carries the identical rule; the two must
# not diverge.
#
# This used to be optional: a bare .\install.ps1 skipped the config step,
# printed a "write one before first use" note, and exited 0. That exit 0 was
# the defect. `init-config` is the surface that MINTS THE INSTALL ID, and the
# id names the config directory, the data directory, and the keychain account.
# An install that never reached it is un-namespaced: `paths` prints an empty
# install id and the first `watch` is refused outright. Since `init-config`
# itself requires --owner-domain, the installer cannot mint one on its own --
# so the only honest options are to demand the domain or to hand back a broken
# install claiming success. It demands the domain.
if (-not $OwnerDomain) {
    # SINGLE-quoted here-string: the text names `paths` and `watch` in
    # backticks, and a double-quoted here-string would read each one as
    # PowerShell's escape character and eat the letter after it.
    Write-Host @'
-OwnerDomain is REQUIRED. Without it this installer cannot write a config,
and an install with no config has no install id -- its `paths` shows an empty
install id and its first `watch` is refused, because an un-namespaced keychain
account would collide with any other install on this machine.

  .\install.ps1 -OwnerDomain example.com

Prefer to be asked instead of typing flags? Run the interactive wizard, which
mints the same id:

  uv run inbox-zero-connector setup

Nothing was installed. Re-run with the flag above.
'@
    exit 2
}

# uv.lock is lock revision 3, which uv 0.8.0 introduced. An older uv on PATH
# takes the "already installed" branch and then fails `uv sync` with a
# lock-format error that says nothing about upgrading.
$UV_MIN_VERSION = [version]"0.8.0"

Step "uv (installs if missing)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
# Get-Command, not $LASTEXITCODE: an unresolvable name raises
# CommandNotFoundException and never sets an exit code, so Assert-Ok after a
# bare `uv --version` would read whatever the PREVIOUS native command left
# behind ($null on a fresh session) and pass or fail for the wrong reason.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "FAILED: uv is still not on PATH after the bootstrap. Open a NEW"
    Write-Host "terminal (the installer added %USERPROFILE%\.local\bin to PATH for"
    Write-Host "this window only) and re-run .\install.ps1"
    exit 1
}
try {
    $uvVersionLine = (& uv --version 2>&1 | ForEach-Object { "$_" }) -join " "
    $uvExit = $LASTEXITCODE
} catch {
    Write-Host "FAILED: uv --version could not run -- $($_.Exception.Message)"
    exit 1
}
Write-Host $uvVersionLine
if ($uvExit -ne 0) {
    Write-Host "FAILED: uv --version (exit $uvExit)"
    exit 1
}
$uvVersion = $null
if ($uvVersionLine -match '(\d+)\.(\d+)\.(\d+)') { $uvVersion = [version]$Matches[0] }
if ($null -eq $uvVersion -or $uvVersion -lt $UV_MIN_VERSION) {
    Write-Host "FAILED: uv $uvVersionLine is older than the required $UV_MIN_VERSION"
    Write-Host "(uv.lock is lock revision 3). Upgrade with:  uv self update"
    exit 1
}

Step "dependencies (uv sync -- also fetches Python 3.14 if needed)"
try {
    uv sync
    $syncExit = $LASTEXITCODE
} catch {
    Write-Host "FAILED: uv sync -- $($_.Exception.Message)"
    exit 1
}
if ($syncExit -ne 0) {
    Write-Host "FAILED: uv sync (exit $syncExit)"
    exit 1
}

Step "folder taxonomy"
Run-Init @("init-taxonomy")

# ALWAYS reached: this is the step that MINTS THE INSTALL ID (#171). The
# -OwnerDomain guard at the top of the try block is what makes that true, so
# there is no un-namespaced path through this installer any more.
Step "config (drafts stay OFF -- triage-only first posture)"
Run-Init @("init-config", "--owner-domain", $OwnerDomain)

# Safe on $LASTEXITCODE here: uv is proven resolvable above, so each of these
# IS a native call that sets it. The try/catch covers the residual case where
# uv vanishes mid-run (uninstalled, PATH edited in another window).
Step "verify"
try {
    uv run inbox-zero-connector --version
    Assert-Ok "version check"
    uv run inbox-zero-connector paths
    Assert-Ok "paths"
    uv run inbox-zero-connector show-scopes
    Assert-Ok "show-scopes"
} catch {
    Write-Host "FAILED: verify -- $($_.Exception.Message)"
    exit 1
}

Step "background service"
Register-Service

Step "machine setup complete"
Write-NextSteps

} finally { Stop-Transcript | Out-Null }
