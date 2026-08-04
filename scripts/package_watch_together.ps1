[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [string]$SourceDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $SourceDirectory) {
    $SourceDirectory = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

$sourceRoot = (Resolve-Path -LiteralPath $SourceDirectory).Path.TrimEnd('\', '/')
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

# Only the application runtime is copied. The update archive is intentionally
# flat at its root so utils/update.py can consume it without leaving a branch
# directory behind. These exclusions are explicit to keep credentials and
# generated state out of every test package.
$excludedDirectoryNames = @(
    '.git', '.codex', 'AGENTS', 'docs', 'tests', 'scripts', '__pycache__',
    '.cache', 'cache', '.tmp', 'logs', 'log'
)
$excludedFilePatterns = @(
    '*.pyc', '*.pyo', '*.log', '*_rooms.json', '*_diff.ini',
    '*_secret*', '*_secrets*', '*_key*', '*token*', '*.bak'
)

$archivePath = Join-Path $outputRoot 'embyToLocalPlayer-watch_together.zip'

function Test-ExcludedFile {
    param([System.IO.FileInfo]$File)
    foreach ($pattern in $excludedFilePatterns) {
        if ($File.Name -like $pattern) { return $true }
    }
    return $false
}

function Copy-PackageFile {
    param([string]$RelativePath)
    $sourcePath = Join-Path $sourceRoot $RelativePath
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Required package file not found: $RelativePath"
    }
    $targetPath = Join-Path $stagingRoot $RelativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $targetPath) -Force | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
}

function Normalize-DirectoryPath {
    param([string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $rootPath = [IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.Length -gt $rootPath.Length) {
        $fullPath = $fullPath.TrimEnd('\', '/')
    }
    return $fullPath
}

$tempBaseResolved = Normalize-DirectoryPath ((Resolve-Path -LiteralPath ([IO.Path]::GetTempPath())).Path)
$stagingName = "etlp-watch_together-" + [guid]::NewGuid().ToString('N')
$stagingRoot = [IO.Path]::GetFullPath((Join-Path $tempBaseResolved $stagingName))

try {
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

    foreach ($rootFile in @(
        'embyToLocalPlayer.py', 'embyToLocalPlayer_config.ini',
        'README.md', 'FUNCTIONS.md', 'LICENSE', 'requirements.txt'
    )) {
        Copy-PackageFile $rootFile
    }
    Copy-PackageFile 'user_script/embyToLocalPlayer.user.js'

    foreach ($directory in @('utils', 'third_party')) {
        $directoryRoot = Join-Path $sourceRoot $directory
        if (-not (Test-Path -LiteralPath $directoryRoot -PathType Container)) {
            throw "Required package directory not found: $directory"
        }
        $files = Get-ChildItem -LiteralPath $directoryRoot -File -Recurse | Where-Object {
            $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart('\', '/')
            $parts = $relative -split '[\\/]'
            $excludedParts = @($parts | Where-Object { $excludedDirectoryNames -contains $_ })
            $excludedParts.Count -eq 0 -and
                -not (Test-ExcludedFile $_)
        }
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($sourceRoot.Length).TrimStart('\', '/')
            Copy-PackageFile $relative
        }
    }

    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    Compress-Archive -Path (Join-Path $stagingRoot '*') -DestinationPath $archivePath -CompressionLevel Optimal
    Write-Output "Created flat watch_together package: $archivePath"
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        if (-not (Test-Path -LiteralPath $stagingRoot -PathType Container)) {
            throw "Refusing to remove non-directory staging path: $stagingRoot"
        }
        $stagingResolved = (Resolve-Path -LiteralPath $stagingRoot).Path
        $stagingParent = Normalize-DirectoryPath ([IO.Path]::GetDirectoryName($stagingResolved))
        $stagingLeaf = [IO.Path]::GetFileName($stagingResolved)
        $validName = $stagingLeaf -match '^etlp-watch_together-[0-9a-fA-F]{32}$'
        $sameParent = [string]::Equals($stagingParent, $tempBaseResolved, [StringComparison]::OrdinalIgnoreCase)
        if (-not ($sameParent -and $validName)) {
            throw "Refusing to remove unverified staging path: $stagingResolved"
        }
        Remove-Item -LiteralPath $stagingResolved -Recurse -Force
    }
}
