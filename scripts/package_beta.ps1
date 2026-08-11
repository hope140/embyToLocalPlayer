[CmdletBinding()]
param(
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $root 'publish'
}

$staging = Join-Path $OutputDirectory 'etlp-remote-control-beta'
$archivePath = Join-Path $OutputDirectory 'etlp-remote-control-beta.zip'
$checksumPath = "$archivePath.sha256"

if (Test-Path -LiteralPath $staging) {
    [System.IO.Directory]::Delete($staging, $true)
}
New-Item -ItemType Directory -Path $staging -Force | Out-Null

$rootFiles = @(
    'embyToLocalPlayer.py',
    'embyToLocalPlayer_config.ini',
    'LICENSE',
    'requirements.txt'
)
foreach ($file in $rootFiles) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination (Join-Path $staging $file) -Force
}

# Keep only Python runtime modules from utils.  The source tree also contains
# development/alternate launchers under utils/others, which are not needed by
# the Windows beta package.
$utilsSource = Join-Path $root 'utils'
$utilsTarget = Join-Path $staging 'utils'
foreach ($file in Get-ChildItem -LiteralPath $utilsSource -Recurse -File |
    Where-Object { $_.Extension -eq '.py' -and $_.FullName -notlike (Join-Path $utilsSource 'others\*') }) {
    $relative = $file.FullName.Substring($utilsSource.Length).TrimStart([char[]]@('\', '/'))
    $destination = Join-Path $utilsTarget $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
}

# The browser userscript is runtime input; keep JavaScript only.
$userScriptSource = Join-Path $root 'user_script'
$userScriptTarget = Join-Path $staging 'user_script'
foreach ($file in Get-ChildItem -LiteralPath $userScriptSource -Recurse -File |
    Where-Object { $_.Extension -eq '.js' }) {
    $relative = $file.FullName.Substring($userScriptSource.Length).TrimStart([char[]]@('\', '/'))
    $destination = Join-Path $userScriptTarget $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
}

# Bundled Python distributions are required by the embedded runtime.  Keep
# wheels only; the source .proto and explanatory README are not runtime input.
$thirdPartySource = Join-Path $root 'third_party'
$thirdPartyTarget = Join-Path $staging 'third_party'
$wheels = @(Get-ChildItem -LiteralPath $thirdPartySource -Filter '*.whl' -File)
if ($wheels.Count -eq 0) {
    throw "No bundled Python wheels found under $thirdPartySource"
}
New-Item -ItemType Directory -Path $thirdPartyTarget -Force | Out-Null
foreach ($wheel in $wheels) {
    Copy-Item -LiteralPath $wheel.FullName -Destination (Join-Path $thirdPartyTarget $wheel.Name) -Force
}

# Ship the Windows launcher at the package root so it can be run directly,
# matching the standalone install layout (it lives under utils/others/ in git).
$launcher = Join-Path $root 'utils\others\embyToLocalPlayer_debug.bat'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Required Windows launcher is missing: $launcher"
}
Copy-Item -LiteralPath $launcher -Destination (Join-Path $staging 'embyToLocalPlayer_debug.bat') -Force

# Remove bytecode caches and other non-runtime files from the package.
Get-ChildItem -LiteralPath $staging -Recurse -Directory |
    Where-Object { $_.Name -eq '__pycache__' } |
    ForEach-Object { [System.IO.Directory]::Delete($_.FullName, $true) }
Get-ChildItem -LiteralPath $staging -Recurse -File |
    Where-Object { $_.Extension -in '.pyc', '.pyo' } |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }

# The embedded Windows runtime extracts binary wheels on first launch.  Never
# copy that machine-generated cache back into a distributable archive.
foreach ($dir in @('_runtime_deps', '_runtime_deps.tmp')) {
    $generated = Join-Path $staging "third_party\$dir"
    if (Test-Path -LiteralPath $generated) {
        [System.IO.Directory]::Delete($generated, $true)
    }
}

if (Test-Path -LiteralPath $archivePath) {
    [System.IO.File]::Delete($archivePath)
}
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $archivePath -Force

$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
$utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllText(
    $checksumPath,
    "$archiveHash  $(Split-Path -Leaf $archivePath)$([Environment]::NewLine)",
    $utf8NoBom
)

Write-Output "==> package folder: $staging"
Write-Output "==> archive: $archivePath"
Write-Output "==> checksum: $checksumPath"
