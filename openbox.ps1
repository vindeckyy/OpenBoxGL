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
        if ($candidate -and ((Test-Path -LiteralPath (Join-Path $candidate 'index.html')) -or (Test-Path -LiteralPath (Join-Path $candidate 'web_app.py')))) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Could not locate OpenBox share files. Set OPENBOX_SHARE to the OpenBox install folder.'
}

function Resolve-ServerBin {
    if ($env:OPENBOX_BACKEND -eq 'python') { return $null }
    $candidates = @()
    if ($env:OPENBOX_SERVER_BIN) { $candidates += $env:OPENBOX_SERVER_BIN }
    $candidates += (Join-Path $script:ShareDir 'openbox-server.exe')
    $candidates += (Join-Path $PSScriptRoot 'openbox-server.exe')
    $candidates += (Join-Path $PSScriptRoot 'target\release\openbox-server.exe')
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $onPath = Get-Command 'openbox-server.exe' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

function Resolve-Python {
    if ($env:OPENBOX_PYTHON) { return $env:OPENBOX_PYTHON }
    foreach ($name in @('python.exe', 'py.exe')) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    throw 'Neither openbox-server.exe nor Python 3 was found. Install Python 3.11+ or set OPENBOX_SERVER_BIN.'
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
    $env:OPENBOX_ROOT = $script:ShareDir
    if ($script:ServerBin) {
        & $script:ServerBin @Forwarded
        exit $LASTEXITCODE
    }
    $python = Resolve-Python
    & $python -B (Join-Path $script:ShareDir 'web_app.py') @Forwarded
    exit $LASTEXITCODE
}

$script:ShareDir = Resolve-ShareDirectory
$script:ServerBin = Resolve-ServerBin
if ($script:ServerBin) {
    $env:OPENBOX_SERVER_BIN = $script:ServerBin
}
$env:OPENBOX_ROOT = $script:ShareDir

# ``--web`` skips the native host entirely and forwards the remaining flags.
# Library maintenance runs headless: no native host, no window.
$libraryCommands = @('--check-library', '--repair-library', '--migrate-library', '--rollback-library')
if (@($Arguments | Where-Object { $libraryCommands -contains $_ }).Count -gt 0) {
    Invoke-WebApp -Forwarded $Arguments
    exit $LASTEXITCODE
}
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
