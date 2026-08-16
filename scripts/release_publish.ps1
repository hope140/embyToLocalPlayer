[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PlanPath,
    [switch]$Execute,
    [string]$Repository = 'hope140/embyToLocalPlayer'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$versionPattern = '^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$'

function Fail([string]$Message) {
    throw "Release Publish 失败：$Message"
}

function Invoke-GitText {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $result = @(& git -C $root @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Fail "无法$Description。"
    }
    return (($result -join "`n").Trim())
}

function Read-ZipEntryText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath,
        [Parameter(Mandatory = $true)]
        [string]$EntryName
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $entry = $archive.GetEntry($EntryName)
        if ($null -eq $entry) {
            Fail "发布包缺少运行时元数据：$EntryName"
        }
        $stream = $entry.Open()
        try {
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8, $true)
            try {
                return $reader.ReadToEnd()
            }
            finally {
                $reader.Dispose()
            }
        }
        finally {
            $stream.Dispose()
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Get-ZipEntryNames {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $names = @()
        foreach ($entry in $archive.Entries) {
            if (-not [string]::IsNullOrWhiteSpace($entry.FullName)) {
                $names += $entry.FullName.Replace('\', '/').TrimStart('/')
            }
        }
        return $names
    }
    finally {
        $archive.Dispose()
    }
}

function Assert-PackageRuntime {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath
    )

    $entryNames = @(Get-ZipEntryNames -ArchivePath $ArchivePath)
    $lowerNames = @($entryNames | ForEach-Object { ([string]$_).ToLowerInvariant() })
    foreach ($requiredEntry in @(
        'python_embed/python.exe',
        'python_embed/python39.dll',
        'python_embed/python39._pth'
    )) {
        if ($lowerNames -notcontains $requiredEntry) {
            Fail "发布包缺少 Python embedded 运行时文件：$requiredEntry"
        }
    }
    $wheelCount = @($lowerNames | Where-Object { $_ -match '^third_party/[^/]+\.whl$' }).Count
    if ($wheelCount -lt 1) {
        Fail '发布包缺少 third_party/*.whl 依赖文件。'
    }
}

if (-not (Test-Path -LiteralPath $PlanPath -PathType Leaf)) {
    Fail "找不到 release-plan.json：$PlanPath"
}
$planPath = [System.IO.Path]::GetFullPath($PlanPath)
$planDirectory = Split-Path -Parent $planPath
if ((Split-Path -Leaf $planPath) -ne 'release-plan.json') {
    Fail 'Plan 文件必须命名为 release-plan.json。'
}

try {
    $plan = Get-Content -LiteralPath $planPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    Fail "无法解析 release-plan.json：$($_.Exception.Message)"
}

$requiredFields = @(
    'schema', 'channel', 'version', 'branch', 'commit', 'packageAsset',
    'checksumAsset', 'packageSha256', 'packageSize'
)
foreach ($field in $requiredFields) {
    if (-not ($plan.PSObject.Properties.Name -contains $field)) {
        Fail "release-plan.json 缺少字段：$field"
    }
}

if ([int]$plan.schema -ne 1) {
    Fail "不支持的 release-plan schema：$($plan.schema)"
}
$channel = [string]$plan.channel
if ($channel -notin @('beta', 'stable')) {
    Fail "不支持的发布频道：$channel"
}
$version = [string]$plan.version
if ($version -notmatch $versionPattern) {
    Fail "版本号格式无效：$version"
}
if ($channel -eq 'beta' -and $version -notmatch '-beta$') {
    Fail "beta 版本必须以 -beta 结尾：$version"
}
if ($channel -eq 'stable' -and $version -match '-beta$') {
    Fail "stable 版本不能以 -beta 结尾：$version"
}

$branch = Invoke-GitText -Arguments @('branch', '--show-current') -Description '读取当前分支'
if ($branch -ne $channel -or [string]$plan.branch -ne $channel) {
    Fail "当前分支、plan 分支和发布频道必须一致：branch=$branch plan=$($plan.branch) channel=$channel"
}
$currentCommit = Invoke-GitText -Arguments @('rev-parse', 'HEAD') -Description '读取当前 commit'
$plannedCommit = ([string]$plan.commit).ToLowerInvariant()
if ($currentCommit -notmatch '^[0-9a-fA-F]{40}$' -or $plannedCommit -notmatch '^[0-9a-f]{40}$') {
    Fail '当前 commit 或 plan commit 不是完整 SHA-1。'
}
if ($currentCommit.ToLowerInvariant() -ne $plannedCommit) {
    Fail "当前 HEAD 与 plan commit 不一致：current=$currentCommit plan=$plannedCommit"
}
$statusLines = @(& git -C $root status --porcelain --untracked-files=all 2>$null)
if ($LASTEXITCODE -ne 0) {
    Fail '无法检查当前工作区状态。'
}
if ($statusLines.Count -gt 0) {
    Fail "当前工作区不是干净状态，拒绝发布：$($statusLines -join '; ')"
}

$expectedPackage = "etlp-remote-control-$channel.zip"
$expectedChecksum = "$expectedPackage.sha256"
if ([string]$plan.packageAsset -ne $expectedPackage -or [string]$plan.checksumAsset -ne $expectedChecksum) {
    Fail 'plan 资产名称与发布频道不一致。'
}
foreach ($assetName in @([string]$plan.packageAsset, [string]$plan.checksumAsset)) {
    if ([System.IO.Path]::GetFileName($assetName) -ne $assetName) {
        Fail "资产路径必须是当前目录下的文件名：$assetName"
    }
}
$packagePath = Join-Path $planDirectory $expectedPackage
$checksumPath = Join-Path $planDirectory $expectedChecksum
if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
    Fail "找不到发布 ZIP：$packagePath"
}
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    Fail "找不到 SHA-256 sidecar：$checksumPath"
}

$packageInfo = Get-Item -LiteralPath $packagePath
$actualHash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ([int64]$plan.packageSize -ne [int64]$packageInfo.Length) {
    Fail "plan 文件大小与实际 ZIP 不一致。"
}
if ([string]$plan.packageSha256.ToLowerInvariant() -ne $actualHash) {
    Fail "plan SHA-256 与实际 ZIP 不一致。"
}
$checksumText = ([System.IO.File]::ReadAllText($checksumPath)).Trim()
if ($checksumText -notmatch '^\s*([0-9a-fA-F]{64})\s+(.+?)\s*$') {
    Fail 'SHA-256 sidecar 格式无效。'
}
$sidecarHash = $Matches[1].ToLowerInvariant()
$sidecarAsset = $Matches[2].Trim()
if ($sidecarHash -ne $actualHash -or $sidecarAsset -ne $expectedPackage) {
    Fail 'SHA-256 sidecar 与实际 ZIP 不一致。'
}
Assert-PackageRuntime -ArchivePath $packagePath

$metadata = Read-ZipEntryText -ArchivePath $packagePath -EntryName 'utils/release_info.py'
$metadataFields = @{
    RELEASE_VERSION = $version
    RELEASE_COMMIT = $plannedCommit.Substring(0, 12)
    RELEASE_CHANNEL = $channel
}
foreach ($field in $metadataFields.Keys) {
    $escaped = [regex]::Escape([string]$metadataFields[$field])
    $pattern = '(?m)^\s*' + $field + '\s*=\s*"' + $escaped + '"\s*$'
    if ([regex]::Matches($metadata, $pattern).Count -ne 1) {
        Fail "ZIP 内 $field 与 release-plan 不一致。"
    }
}

$notesPath = Join-Path $planDirectory 'release-notes.md'
if (-not (Test-Path -LiteralPath $notesPath -PathType Leaf)) {
    Fail "找不到 Release notes 文件：$notesPath"
}
$notesText = [System.IO.File]::ReadAllText($notesPath)
if ([string]::IsNullOrWhiteSpace($notesText)) {
    Fail 'Release notes 文件为空。'
}
foreach ($forbiddenPhrase in @('发布说明草稿', '仅供审核', '请在此处补充', '发布前检查')) {
    if ($notesText.Contains($forbiddenPhrase)) {
        Fail "Release notes 仍包含内部占位内容：$forbiddenPhrase"
    }
}

$title = "etlp $channel $version"
$publishFlags = if ($channel -eq 'beta') { '--prerelease' } else { '--latest' }
$displayAssets = @($expectedPackage, $expectedChecksum, 'release-plan.json')
Write-Output '==> Release plan validated'
Write-Output "==> channel: $channel"
Write-Output "==> version: $version"
Write-Output "==> commit: $plannedCommit"
Write-Output "==> package SHA256: $actualHash"
Write-Output "==> assets: $($displayAssets -join ', ')"

if (-not $Execute) {
    Write-Output '==> DRY-RUN: no gh command, tag, push, or remote Release mutation was performed'
    Write-Output "==> execute command: gh release create $version $expectedPackage $expectedChecksum release-plan.json --repo $Repository --verify-tag --title `"$title`" --notes-file release-notes.md $publishFlags"
    exit 0
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Fail '找不到 gh CLI，无法执行远端发布。'
}

$remoteTags = @(& gh api "repos/$Repository/releases" --paginate --jq '.[].tag_name' 2>$null)
if ($LASTEXITCODE -ne 0) {
    Fail '无法读取远端 Release 列表，已停止发布。'
}
if ($remoteTags -contains $version) {
    Fail "远端已存在 Release $version，拒绝覆盖。"
}

$ghArguments = @(
    'release', 'create', $version,
    $packagePath,
    $checksumPath,
    $planPath,
    '--repo', $Repository,
    '--verify-tag',
    '--title', $title,
    '--notes-file', $notesPath,
    $publishFlags
)
Write-Output "==> executing gh release create for $Repository/$version"
& gh @ghArguments
if ($LASTEXITCODE -ne 0) {
    Fail "gh release create 失败，退出码：$LASTEXITCODE"
}
Write-Output '==> remote Release created; tag and branches were not created or pushed by this script'
