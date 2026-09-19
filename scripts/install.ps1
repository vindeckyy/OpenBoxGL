# Install a signed OpenBoxGL release on Windows.
#
# The Windows counterpart of scripts/install.sh: the same verification ladder,
# the same fail-closed behaviour, and the same user-facing progress lines, but
# run with Windows PowerShell 5.1 and the stdlib alone, so the installer needs
# neither curl nor OpenSSL. Verification is delegated to updates.py, the same
# RFC 8032 implementation the in-app updater uses.
#
# Trust model: unless -PublicKeyPath is given, the release public key is
# downloaded from the release and its SHA-256 MUST match the pinned bootstrap
# anchor below (the value install.sh pins). -PublicKeyPath is the explicit
# mirror/offline escape hatch: the supplied key replaces that anchor check, and
# the .sha256 sidecar plus the signature check still gate the archive.
#
# Windows PowerShell 5.1 compatible (no PowerShell 7-only syntax).

[CmdletBinding()]
param(
    # Launch OpenBox after a successful install.
    [switch] $Run,
    # Skip adding the install to the user PATH.
    [switch] $NoPathUpdate,
    # Bin root; the runtime lands in <InstallDir>\share\openbox.
    [string] $InstallDir,
    # Base URL the release assets are served from.
    [string] $ReleaseBase,
    # Release tag to install; the latest release is looked up when omitted.
    [string] $Tag,
    [string] $Repo = 'vindeckyy/OpenBoxGL',
    # Local release public key file (mirror/offline escape hatch).
    [string] $PublicKeyPath,
    # Artifact architecture; defaults to the host CPU.
    [string] $Arch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# The progress bar makes Invoke-WebRequest an order of magnitude slower.
$ProgressPreference = 'SilentlyContinue'

# Bootstrap trust anchor for the committed production release key.
$ReleaseKeySha256 = '39db9c52aa2e6e24b06ac845c29e0aeda60f3facf1ee30007913d7e93368d9ea'
$KeyAsset = 'openbox-release.pub'

# install.sh gets this from curl's --tlsv1.2; .NET negotiates whatever the OS
# offers, so the HTTPS release base is pinned to TLS 1.2 explicitly.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Say {
    param([string] $Message)
    Write-Host $Message -ForegroundColor Green
}

function Die {
    param([string] $Message)
    # The message goes to stderr and the installer stops. Continue keeps
    # Write-Error from terminating first, so exit 1 and the temp-dir cleanup
    # in the finally block always run.
    Write-Error -Message $Message -ErrorAction Continue
    exit 1
}

function Fetch {
    param([string] $Uri, [string] $Path, [string] $Failure)
    try {
        Invoke-WebRequest -Uri $Uri -OutFile $Path -UseBasicParsing
    } catch {
        Die $Failure
    }
}

function Resolve-Python {
    if ($env:OPENBOX_PYTHON) { return $env:OPENBOX_PYTHON }
    foreach ($name in @('python.exe', 'py.exe')) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    Die 'No Python 3 interpreter found. Install Python 3.10+ or set OPENBOX_PYTHON.'
}

function Resolve-Arch {
    $candidate = $Arch
    if (-not $candidate) { $candidate = $env:OPENBOX_ARCH }
    if (-not $candidate) { $candidate = $env:PROCESSOR_ARCHITECTURE }
    switch ("$candidate".ToLowerInvariant()) {
        'x86_64' { return 'x86_64' }
        'amd64' { return 'x86_64' }
        'aarch64' { return 'aarch64' }
        'arm64' { return 'aarch64' }
        default { Die "Unsupported architecture: '$candidate' (expected x86_64 or aarch64)." }
    }
}

function Resolve-Tag {
    if ($Tag) { return $Tag }
    if ($env:OPENBOX_RELEASE_TAG) { return $env:OPENBOX_RELEASE_TAG }
    Say 'Looking up the latest release...'
    $release = $null
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing
    } catch {
        Die 'Could not resolve the latest release. Check your network connection.'
    }
    $resolved = ''
    if ($release -and $release.PSObject.Properties['tag_name']) { $resolved = "$($release.tag_name)" }
    if (-not $resolved) { Die 'The latest release has no tag.' }
    return $resolved
}

function Get-Sha256 {
    param([string] $Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$verifier = Join-Path $repoRoot 'updates.py'
if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
    Die "The release verifier was not found at $verifier. Run this script from a checkout."
}

$python = Resolve-Python
$arch = Resolve-Arch
$asset = "OpenBox-$arch-windows.zip"
$sigAsset = "$asset.sig"

if (-not $InstallDir) { $InstallDir = $env:OPENBOX_INSTALL_DIR }
if (-not $InstallDir) {
    if (-not $env:LOCALAPPDATA) { Die 'LOCALAPPDATA is not set. Pass -InstallDir.' }
    $InstallDir = Join-Path $env:LOCALAPPDATA 'OpenBox'
}
$installRoot = [System.IO.Path]::GetFullPath($InstallDir)
$shareRoot = Join-Path $installRoot 'share'
$target = Join-Path $shareRoot 'openbox'
$previous = Join-Path $shareRoot 'openbox.previous'

if (-not $ReleaseBase) { $ReleaseBase = $env:OPENBOX_RELEASE_BASE }
if (-not $ReleaseBase) { $ReleaseBase = "https://github.com/$Repo/releases/download" }
$base = $ReleaseBase.TrimEnd('/')

# A release tag is only used in download URLs after it is known to be a
# semantic-version tag; OPENBOX_RELEASE_TAG pins a manually reviewed tag.
$tag = "$(Resolve-Tag)".Trim()
if ($tag -cnotmatch '^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$') {
    Die 'The release tag is not a valid semantic-version tag.'
}
Say "Found release $tag"

$work = Join-Path ([System.IO.Path]::GetTempPath()) ('openbox-install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null

try {
    Say 'Downloading signed release assets...'
    $zip = Join-Path $work $asset
    $sidecar = Join-Path $work "$asset.sha256"
    $signature = Join-Path $work $sigAsset
    Fetch -Uri "$base/$tag/$asset" -Path $zip -Failure 'The release archive download failed.'
    Fetch -Uri "$base/$tag/$asset.sha256" -Path $sidecar -Failure 'Could not fetch the SHA-256 checksum.'
    Fetch -Uri "$base/$tag/$sigAsset" -Path $signature -Failure 'Could not fetch the Ed25519 signature.'

    # A supplied key is the mirror/offline path: the caller vouches for it, so
    # the anchor does not apply. Otherwise the key comes from the release and
    # must hash to the pinned anchor before it is used for anything.
    if ($PublicKeyPath) {
        if (-not (Test-Path -LiteralPath $PublicKeyPath -PathType Leaf)) {
            Die "The release public key was not found at $PublicKeyPath."
        }
        $key = (Resolve-Path -LiteralPath $PublicKeyPath).Path
        Say "Using the supplied release public key $key"
    } else {
        $key = Join-Path $work $KeyAsset
        Fetch -Uri "$base/$tag/$KeyAsset" -Path $key -Failure 'Could not fetch the release public key.'
        if ((Get-Sha256 $key) -ne $ReleaseKeySha256) {
            Die 'The release public key does not match the pinned trust anchor.'
        }
    }

    Say 'Verifying the release key and archive checksum...'
    # The sidecar is "<hex digest>  <asset>" (sha256sum format); only the first
    # token is the digest.
    $tokens = @(((Get-Content -LiteralPath $sidecar -Raw) -split '\s+') | Where-Object { $_ -ne '' })
    $expected = ''
    if ($tokens.Count -gt 0) { $expected = $tokens[0] }
    if ($expected -cnotmatch '^[0-9a-fA-F]{64}$') { Die 'The SHA-256 checksum file is invalid.' }
    if ((Get-Sha256 $zip) -ne $expected.ToLowerInvariant()) {
        Die 'The release archive does not match the published SHA-256 checksum.'
    }

    Say 'Verifying the Ed25519 release signature...'
    # The signer signs the raw SHA-256 digest; updates.py prints the digest it
    # verified and fails non-zero on a corrupt archive, a wrong key, or a
    # malformed signature.
    [string] $verified = & $python -B $verifier verify $zip $signature $key
    if ($LASTEXITCODE -ne 0) { Die 'The Ed25519 release signature verification failed.' }
    Say "Verified SHA-256 $($verified.Trim())"

    # Nothing is extracted before every check above has passed.
    Say 'Extracting the release archive...'
    $staging = Join-Path $work 'extracted'
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        Expand-Archive -LiteralPath $zip -DestinationPath $staging -Force
    } catch {
        Die 'Could not extract the release archive.'
    }
    $entries = @(Get-ChildItem -LiteralPath $staging -Force)
    $folders = @($entries | Where-Object { $_.PSIsContainer })
    $files = @($entries | Where-Object { -not $_.PSIsContainer })
    if ($folders.Count -ne 1 -or $files.Count -ne 0) {
        Die 'The release archive must contain exactly one top-level folder.'
    }
    $tree = $folders[0].FullName
    if (-not (Test-Path -LiteralPath (Join-Path $tree 'web_app.py') -PathType Leaf)) {
        Die 'The release archive does not contain web_app.py.'
    }

    New-Item -ItemType Directory -Path $shareRoot -Force | Out-Null
    if (Test-Path -LiteralPath $target) {
        if (Test-Path -LiteralPath $previous) { Remove-Item -LiteralPath $previous -Recurse -Force }
        Move-Item -LiteralPath $target -Destination $previous
        Say "Kept the previous install at $previous"
    }
    Move-Item -LiteralPath $tree -Destination $target
    Say "Installed to $target"

    Say 'Registering the Start Menu shortcut and the openbox:// protocol...'
    & $python -B (Join-Path $target 'updates.py') install-desktop-entry (Join-Path $target 'openbox.cmd')
    if ($LASTEXITCODE -ne 0) { Die 'Could not register the desktop integration.' }

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    $onPath = $false
    foreach ($entry in @($userPath -split ';')) {
        if ($entry.Trim().TrimEnd('\') -ieq $target.TrimEnd('\')) { $onPath = $true }
    }
    if ($NoPathUpdate) {
        if (-not $onPath) { Say "Note: $target is not on your PATH. Re-run without -NoPathUpdate to add it." }
    } elseif (-not $onPath) {
        $updated = @($userPath -split ';' | Where-Object { $_ -ne '' }) + $target
        [Environment]::SetEnvironmentVariable('Path', ($updated -join ';'), 'User')
        Say "Added $target to your user PATH."
    }

    if ($Run) {
        Say 'Launching OpenBox...'
        & (Join-Path $target 'openbox.cmd')
        exit $LASTEXITCODE
    }

    Say "Done. Run 'openbox' (or $(Join-Path $target 'openbox.cmd')) to start OpenBox."
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
