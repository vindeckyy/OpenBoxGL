# Remove an OpenBoxGL install made by scripts/install.ps1 on Windows.
#
# Removes exactly what install.ps1 (and `updates.py install-desktop-entry`) creates:
#   - <InstallDir>\share\openbox and its openbox.previous rollback copy
#   - the install's entry in the user PATH
#   - the Start Menu shortcut (OpenBox.lnk)
#   - the openbox:// protocol registration (HKCU\Software\Classes\openbox)
# Your library and settings live in the data folder (Settings > About shows where) and are
# never touched: reinstalling picks them straight back up.
#
# Windows PowerShell 5.1 compatible (no PowerShell 7-only syntax).

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Bin root the installer used; defaults to %LOCALAPPDATA%\OpenBox.
    [string] $InstallDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Say {
    param([string] $Message)
    Write-Host $Message -ForegroundColor Green
}

if (-not $InstallDir) { $InstallDir = $env:OPENBOX_INSTALL_DIR }
if (-not $InstallDir) {
    if (-not $env:LOCALAPPDATA) {
        Write-Error -Message 'LOCALAPPDATA is not set. Pass -InstallDir.' -ErrorAction Continue
        exit 1
    }
    $InstallDir = Join-Path $env:LOCALAPPDATA 'OpenBox'
}

$installRoot = [System.IO.Path]::GetFullPath($InstallDir)
$shareRoot = Join-Path $installRoot 'share'
$target = Join-Path $shareRoot 'openbox'
$previous = Join-Path $shareRoot 'openbox.previous'

foreach ($tree in @($target, $previous)) {
    if (Test-Path -LiteralPath $tree) {
        if ($PSCmdlet.ShouldProcess($tree, 'Remove install tree')) {
            Remove-Item -LiteralPath $tree -Recurse -Force
            Say "Removed $tree"
        }
    }
}

# Drop the now-empty share\ and install roots, but only if nothing else lives there.
foreach ($dir in @($shareRoot, $installRoot)) {
    if ((Test-Path -LiteralPath $dir) -and -not (Get-ChildItem -LiteralPath $dir -Force)) {
        Remove-Item -LiteralPath $dir -Force
    }
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
    $kept = @($userPath -split ';' | Where-Object { $_ -ne '' -and $_.Trim().TrimEnd('\') -ine $target.TrimEnd('\') })
    if ($kept.Count -ne @($userPath -split ';' | Where-Object { $_ -ne '' }).Count) {
        if ($PSCmdlet.ShouldProcess('user PATH', 'Remove the OpenBox entry')) {
            [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
            Say 'Removed OpenBox from your user PATH.'
        }
    }
}

$programs = Join-Path ([Environment]::GetFolderPath('Programs')) 'OpenBox.lnk'
if (Test-Path -LiteralPath $programs) {
    if ($PSCmdlet.ShouldProcess($programs, 'Remove Start Menu shortcut')) {
        Remove-Item -LiteralPath $programs -Force
        Say 'Removed the Start Menu shortcut.'
    }
}

$protocolKey = 'HKCU:\Software\Classes\openbox'
if (Test-Path -LiteralPath $protocolKey) {
    if ($PSCmdlet.ShouldProcess($protocolKey, 'Remove openbox:// protocol registration')) {
        Remove-Item -LiteralPath $protocolKey -Recurse -Force
        Say 'Removed the openbox:// protocol registration.'
    }
}

Say 'Done. Your library and settings were left in place.'
