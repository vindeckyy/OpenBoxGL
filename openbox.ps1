# OpenBox launcher for Windows.
#
# Desktop sessions open the native host by default (the WebView2 window). Pass
# --web to fall back to the loopback web server in the default browser.
#
# Behaviourally identical to openbox.sh: same share-dir resolution, same
# native-host-then-web-app fallback ladder, and every CLI flag is forwarded.
# Windows PowerShell 5.1 compatible (no PowerShell 7-only syntax).

[CmdletBinding()]
param(
    [string] $Share,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-ShareDirectory {
    $candidates = @()
    if ($Share) { $candidates += $Share }
    if ($env:OPENBOX_SHARE) { $candidates += $env:OPENBOX_SHARE }
    $candidates += $PSScriptRoot
    $candidates += (Join-Path $PSScriptRoot '..\share\openbox')
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'OpenBox\share\openbox') }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'web_app.py'))) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Could not locate web_app.py. Set OPENBOX_SHARE to the OpenBox install folder.'
}

function Resolve-Python {
    if ($env:OPENBOX_PYTHON) { return $env:OPENBOX_PYTHON }
    foreach ($name in @('python.exe', 'py.exe')) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    throw 'No Python 3 interpreter found. Install Python 3.10+ or set OPENBOX_PYTHON.'
}

function Find-NativeHost {
    # Mirrors openbox.sh: the repo keeps the native launcher beside this script,
    # an install keeps it in the share dir, and PATH covers custom prefixes. The
    # bare host binary is deliberately not a candidate — it is a GUI-subsystem
    # executable, which a launcher cannot wait on or read an exit code from.
    $candidates = @(
        (Join-Path $PSScriptRoot 'openbox-native.ps1'),
        (Join-Path $script:ShareDir 'openbox-native.ps1')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    foreach ($name in @('openbox-native', 'openbox-native.ps1', 'openbox-native.cmd')) {
        $onPath = Get-Command $name -ErrorAction SilentlyContinue
        if ($onPath) { return $onPath.Source }
    }
    return $null
}

function Invoke-WebApp {
    param([string[]] $Forwarded)
    & $script:Python -B (Join-Path $script:ShareDir 'web_app.py') @Forwarded
    exit $LASTEXITCODE
}

$script:ShareDir = Resolve-ShareDirectory
$script:Python = Resolve-Python

# ``--web`` skips the native host entirely and forwards the remaining flags.
if ($Arguments.Count -gt 0 -and $Arguments[0] -eq '--web') {
    $forwarded = @()
    if ($Arguments.Count -gt 1) { $forwarded = $Arguments[1..($Arguments.Count - 1)] }
    Invoke-WebApp -Forwarded $forwarded
}

$nativeHost = Find-NativeHost
if ($nativeHost) {
    & $nativeHost @Arguments
    exit $LASTEXITCODE
}

Write-Warning 'openbox-native launcher not found; falling back to the web app.'
Invoke-WebApp -Forwarded $Arguments
