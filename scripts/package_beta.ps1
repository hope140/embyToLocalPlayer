[CmdletBinding()]
param(
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $root 'dist'
}

$staging = Join-Path $OutputDirectory 'etlp-remote-control-beta'
$archivePath = Join-Path $OutputDirectory 'etlp-remote-control-beta.zip'

if (Test-Path -LiteralPath $staging) {
    [System.IO.Directory]::Delete($staging, $true)
}
New-Item -ItemType Directory -Path $staging -Force | Out-Null

$rootFiles = @(
    'embyToLocalPlayer.py',
    'embyToLocalPlayer_config.ini',
    'README.md',
    'FUNCTIONS.md',
    'LICENSE',
    'requirements.txt'
)
foreach ($file in $rootFiles) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination (Join-Path $staging $file) -Force
}

foreach ($dir in @('utils', 'third_party', 'user_script')) {
    Copy-Item -Path (Join-Path $root $dir) -Destination $staging -Recurse -Force
}

# Remove bytecode caches and other non-runtime files from the package.
Get-ChildItem -LiteralPath $staging -Recurse -Directory |
    Where-Object { $_.Name -eq '__pycache__' } |
    ForEach-Object { [System.IO.Directory]::Delete($_.FullName, $true) }
Get-ChildItem -LiteralPath $staging -Recurse -File |
    Where-Object { $_.Extension -in '.pyc', '.pyo' } |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }

if (Test-Path -LiteralPath $archivePath) {
    [System.IO.File]::Delete($archivePath)
}
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $archivePath -Force

Write-Output "==> package folder: $staging"
Write-Output "==> archive: $archivePath"
