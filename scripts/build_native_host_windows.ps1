# Build the Windows native host (WebView2).
#
# Produces native_host.exe next to web_app.py, embedding openbox.ico as the
# executable's icon resource. The host itself loads openbox.ico at runtime for
# the window and tray icon, so both files ship together.
#
# Requires the MSVC toolchain (Visual Studio Build Tools with the C++ workload)
# and the WebView2 SDK. The SDK is taken from the NuGet cache when present and
# downloaded from nuget.org otherwise. WebView2LoaderStatic.lib is linked
# statically, so the resulting binary needs no WebView2Loader.dll at runtime.

[CmdletBinding()]
param(
    [string] $OutputPath,
    [string] $SdkVersion = '1.0.2651.64'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repoRoot 'native_host_win.c'
if (-not (Test-Path -LiteralPath $source)) {
    throw "native_host_win.c not found at $source"
}

function Find-VcVars {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path -LiteralPath $vswhere) {
        $install = & $vswhere -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath
        if ($install) {
            $candidate = Join-Path $install 'VC\Auxiliary\Build\vcvars64.bat'
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        $pattern = Join-Path $base 'Microsoft Visual Studio\*\*\VC\Auxiliary\Build\vcvars64.bat'
        $found = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw 'vcvars64.bat not found. Install Visual Studio Build Tools with the "Desktop development with C++" workload.'
}

function Find-WebView2Sdk {
    $packages = Join-Path $env:USERPROFILE '.nuget\packages\microsoft.web.webview2'
    $native = Join-Path $packages "$SdkVersion\build\native"
    if ((Test-Path -LiteralPath (Join-Path $native 'include\WebView2.h')) -and
        (Test-Path -LiteralPath (Join-Path $native 'x64\WebView2LoaderStatic.lib'))) {
        return $native
    }

    # Not cached: fetch the .nupkg (a zip) and expand just the native build tree.
    Write-Host "Downloading WebView2 SDK $SdkVersion from nuget.org..."
    $staging = Join-Path ([System.IO.Path]::GetTempPath()) "openbox-webview2-$SdkVersion"
    $nupkg = "$staging.nupkg"
    $url = "https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/$SdkVersion"
    Invoke-WebRequest -Uri $url -OutFile $nupkg -UseBasicParsing

    if (Test-Path -LiteralPath $staging) { Remove-Item -Recurse -Force -LiteralPath $staging }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($nupkg, $staging)
    Remove-Item -Force -LiteralPath $nupkg

    $native = Join-Path $staging 'build\native'
    if (-not (Test-Path -LiteralPath (Join-Path $native 'include\WebView2.h'))) {
        throw "WebView2 SDK download did not contain build\native\include\WebView2.h"
    }
    return $native
}

$vcvars = Find-VcVars
$sdk = Find-WebView2Sdk
if (-not $OutputPath) { $OutputPath = Join-Path $repoRoot 'native_host.exe' }

$icon = Join-Path $repoRoot 'openbox.ico'
$resource = Join-Path $repoRoot 'native_host_win.rc'
$objects = Join-Path ([System.IO.Path]::GetTempPath()) 'openbox-native-host-obj'
if (Test-Path -LiteralPath $objects) { Remove-Item -Recurse -Force -LiteralPath $objects }
New-Item -ItemType Directory -Path $objects | Out-Null

$iconArg = if (Test-Path -LiteralPath $icon) { "`"$icon`"" } else { '' }
$resourceArg = if ($iconArg -and (Test-Path -LiteralPath $resource)) { "`"$resource`"" } else { '' }
if (-not $resourceArg) {
    Write-Warning "openbox.ico or native_host_win.rc missing; building without an embedded icon."
}

# cl.exe, rc.exe and their environment only exist inside the vcvars shell.
# rc.exe compiles the icon resource to a .res that the linker consumes; passing
# the .rc straight to cl makes it treat the file as an object and fail.
$resourceObj = Join-Path $objects 'native_host_win.res'
$compileResource = if ($resourceArg) {
    "rc /nologo /fo `"$resourceObj`" `"$resource`""
} else {
    ''
}
$linkResource = if ($resourceArg) { "`"$resourceObj`"" } else { '' }

$steps = @("call `"$vcvars`" >nul")
if ($compileResource) { $steps += $compileResource }
# _CRT_SECURE_NO_WARNINGS: the host uses the portable C string functions
# (wcscpy, sscanf, _snwprintf) that the Linux host uses too, so MSVC's
# deprecation notes are noise rather than a finding.
$steps += "cl /nologo /O2 /W3 /DUNICODE /D_UNICODE /D_CRT_SECURE_NO_WARNINGS `"$source`" $linkResource /Fo:`"$objects\\`" /Fe:`"$OutputPath`" /I`"$sdk\include`" `"$sdk\x64\WebView2LoaderStatic.lib`" ole32.lib oleaut32.lib uuid.lib user32.lib gdi32.lib shell32.lib shlwapi.lib advapi32.lib ws2_32.lib oleacc.lib /link /SUBSYSTEM:WINDOWS"
$command = $steps -join ' && '

Write-Host "Building native_host.exe..."
& cmd.exe /c $command
if ($LASTEXITCODE -ne 0) { throw "native_host build failed with exit code $LASTEXITCODE" }

Remove-Item -Recurse -Force -LiteralPath $objects
Write-Host "Built $OutputPath"
