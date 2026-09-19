# OpenBox native host launcher for Windows.
#
# Runs the WebView2 host, which spawns the Python web server and renders the
# one UI in a native window. Mirrors openbox-native.sh: the same share-dir
# resolution, the OPENBOX_NATIVE_HOST / OPENBOX_PYTHON overrides, and the same
# fallback to the web app when the native host is missing or fails.
# Windows PowerShell 5.1 compatible (no PowerShell 7-only syntax).

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-ShareDirectory {
    $candidates = @()
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

function Invoke-WebApp {
    & $script:Python -B (Join-Path $script:ShareDir 'web_app.py') @Arguments
    exit $LASTEXITCODE
}

function ConvertTo-ArgumentList {
    param([string[]] $Values)
    # Start-Process joins its -ArgumentList with spaces and never quotes what it
    # is given, so values carrying spaces or quotes must be quoted here. POSIX
    # gets this for free from "$@".
    $quoted = foreach ($value in $Values) {
        if ($value -match '[\s"]') { '"' + ($value -replace '(\\*)"', '$1$1\"') + '"' } else { $value }
    }
    return ($quoted -join ' ')
}

$script:ShareDir = Resolve-ShareDirectory
$script:Python = Resolve-Python
$script:HostBinary = Join-Path $script:ShareDir 'native_host.exe'
if ($env:OPENBOX_NATIVE_HOST) { $script:HostBinary = $env:OPENBOX_NATIVE_HOST }

if (-not (Test-Path -LiteralPath $script:HostBinary)) {
    Write-Warning "native_host is missing at $script:HostBinary; falling back to the system-browser app window."
    Invoke-WebApp
}

$env:OPENBOX_WEB_APP = Join-Path $script:ShareDir 'web_app.py'
$env:OPENBOX_PYTHON = $script:Python
# The host is a GUI-subsystem binary: ``&`` neither waits for it nor sets
# $LASTEXITCODE, and the fallback below must not run while a window is up.
$start = @{ FilePath = $script:HostBinary; Wait = $true; PassThru = $true }
if ($Arguments.Count -gt 0) { $start['ArgumentList'] = ConvertTo-ArgumentList $Arguments }
$hostProcess = Start-Process @start
$code = $hostProcess.ExitCode
if ($code -eq 0) { exit 0 }

Write-Warning "native_host failed (exit $code). Install the WebView2 runtime: https://developer.microsoft.com/microsoft-edge/webview2/"
Write-Warning 'Falling back to the system-browser app window.'
Invoke-WebApp
