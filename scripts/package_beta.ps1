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
    Copy-Item -LiteralPath (Join-Path $root $dir) -Destination (Join-Path $staging $dir) -Recurse -Force
}

if (Test-Path -LiteralPath $archivePath) {
    [System.IO.File]::Delete($archivePath)
}
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $archivePath -Force

Write-Output "==> package folder: $staging"
Write-Output "==> archive: $archivePath"
