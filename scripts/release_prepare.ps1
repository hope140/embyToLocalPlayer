[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Alias('Version')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$')]
    [string]$ReleaseVersion,
    [ValidateSet('beta', 'stable')]
    [string]$Channel,
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false

function Invoke-GitText {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $result = @(& git -C $root @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "无法$Description。请确认当前目录是 Git 仓库且 git 可用。"
    }
    return (($result -join "`n").Trim())
}

function Assert-VersionChannel {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExpectedChannel,
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    if ($ExpectedChannel -eq 'beta' -and $Version -notmatch '-beta$') {
        throw "beta 版本必须以 -beta 结尾：$Version"
    }
    if ($ExpectedChannel -eq 'stable' -and $Version -match '-beta$') {
        throw "stable 版本不能以 -beta 结尾：$Version"
    }
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
            throw "发布包缺少运行时元数据：$EntryName"
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

function Assert-PackageMetadata {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedChannel,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedVersion,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedCommitShort
    )

    $metadata = Read-ZipEntryText -ArchivePath $ArchivePath -EntryName 'utils/release_info.py'
    $expectedFields = @{
        RELEASE_VERSION = $ExpectedVersion
        RELEASE_COMMIT = $ExpectedCommitShort
        RELEASE_CHANNEL = $ExpectedChannel
    }
    foreach ($field in $expectedFields.Keys) {
        $escapedValue = [regex]::Escape([string]$expectedFields[$field])
        $pattern = '(?m)^\s*' + $field + '\s*=\s*"' + $escapedValue + '"\s*$'
        if ([regex]::Matches($metadata, $pattern).Count -ne 1) {
            throw "发布包中的 $field 与本次准备信息不一致。"
        }
    }
}

try {
    $branch = Invoke-GitText -Arguments @('branch', '--show-current') -Description '读取当前分支'
    if ([string]::IsNullOrWhiteSpace($branch)) {
        throw "当前 HEAD 处于 detached 状态，Release Prepare 只允许从 beta 或 stable 分支执行。"
    }

    if ([string]::IsNullOrWhiteSpace($Channel)) {
        if ($branch -notin @('beta', 'stable')) {
            throw "无法从当前分支 '$branch' 自动确定频道；请切换到 beta/stable 分支后重试。"
        }
        $Channel = $branch
    }
    if ($branch -ne $Channel) {
        throw "拒绝从分支 '$branch' 准备频道 '$Channel'；请先切换到 '$Channel' 分支。"
    }
    Assert-VersionChannel -ExpectedChannel $Channel -Version $ReleaseVersion

    $statusLines = @(& git -C $root status --porcelain --untracked-files=all 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw '无法检查 Git 工作区状态。'
    }
    if ($statusLines.Count -gt 0) {
        $statusPreview = (($statusLines | Select-Object -First 12) -join "`n")
        if ($statusLines.Count -gt 12) {
            $statusPreview += "`n...（其余 $($statusLines.Count - 12) 项未显示）"
        }
        throw "工作区不是干净状态，已拒绝生成 Release Prepare 产物。请先提交或单独保存以下改动：`n$statusPreview"
    }

    $commit = Invoke-GitText -Arguments @('rev-parse', 'HEAD') -Description '读取当前 commit'
    if ($commit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "当前 commit 不是有效的完整 SHA-1：$commit"
    }
    $commit = $commit.ToLowerInvariant()
    $commitShort = $commit.Substring(0, 12)

    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        $OutputDirectory = Join-Path $root 'publish'
    }
    $OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

    $wrapper = Join-Path $PSScriptRoot ("package_{0}.ps1" -f $Channel)
    if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
        throw "找不到频道打包入口：$wrapper"
    }

    Write-Output "==> 准备本地 $Channel Release $ReleaseVersion"
    Write-Output "==> branch: $branch"
    Write-Output "==> commit: $commit"
    Write-Output "==> output: $OutputDirectory"

    & $wrapper -OutputDirectory $OutputDirectory -ReleaseVersion $ReleaseVersion
    $wrapperExitCode = $LASTEXITCODE
    if ($wrapperExitCode -ne 0) {
        throw "频道打包失败，退出码：$wrapperExitCode"
    }

    $packageAsset = "etlp-remote-control-$Channel.zip"
    $checksumAsset = "$packageAsset.sha256"
    $archivePath = Join-Path $OutputDirectory $packageAsset
    $checksumPath = Join-Path $OutputDirectory $checksumAsset
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "频道打包完成但未找到 ZIP：$archivePath"
    }
    if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
        throw "频道打包完成但未找到 SHA-256 sidecar：$checksumPath"
    }

    $archiveHash = ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash).ToLowerInvariant()
    $archiveInfo = Get-Item -LiteralPath $archivePath
    $checksumText = ([System.IO.File]::ReadAllText($checksumPath)).Trim()
    if ($checksumText -notmatch '^\s*([0-9a-fA-F]{64})\s+(.+?)\s*$') {
        throw "SHA-256 sidecar 格式无效：$checksumPath"
    }
    $sidecarHash = $Matches[1].ToLowerInvariant()
    $sidecarAsset = $Matches[2].Trim()
    if ($sidecarHash -ne $archiveHash -or $sidecarAsset -ne $packageAsset) {
        throw "SHA-256 sidecar 与实际 ZIP 不一致：$checksumPath"
    }
    Assert-PackageMetadata -ArchivePath $archivePath -ExpectedChannel $Channel `
        -ExpectedVersion $ReleaseVersion -ExpectedCommitShort $commitShort

    $plan = [ordered]@{
        schema = 1
        channel = $Channel
        version = $ReleaseVersion
        branch = $branch
        commit = $commit
        commitShort = $commitShort
        packageAsset = $packageAsset
        checksumAsset = $checksumAsset
        packageSha256 = $archiveHash
        packageSize = [int64]$archiveInfo.Length
        assets = @(
            [ordered]@{
                name = $packageAsset
                kind = 'package'
                sha256 = $archiveHash
                size = [int64]$archiveInfo.Length
            },
            [ordered]@{
                name = $checksumAsset
                kind = 'sha256'
                size = [int64](Get-Item -LiteralPath $checksumPath).Length
            }
        )
    }
    $planPath = Join-Path $OutputDirectory 'release-plan.json'
    $notesPath = Join-Path $OutputDirectory 'release-notes.md'
    $planJson = $plan | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($planPath, $planJson + [Environment]::NewLine, $utf8NoBom)

    $sizeMiB = ([double]$archiveInfo.Length / 1MB).ToString('0.00', [Globalization.CultureInfo]::InvariantCulture)
    $notes = @(
        '# ETLP Release 发布说明草稿',
        '',
        '> 此文件由 `scripts/release_prepare.ps1` 在本地生成，仅供审核；脚本不会 push、创建 tag 或发布 GitHub Release。',
        '',
        '## 发布信息',
        '',
        "- 频道：$Channel",
        "- 版本：$ReleaseVersion",
        "- 分支：$branch",
        ('- Commit：`{0}`' -f $commit),
        ('- 正式包：`{0}`（{1} MiB）' -f $packageAsset, $sizeMiB),
        ('- SHA-256：`{0}`' -f $archiveHash),
        ('- 校验文件：`{0}`' -f $checksumAsset),
        '',
        '## 发布前检查',
        '',
        '- [ ] 确认 Release tag 指向上面的 commit。',
        "- [ ] $Channel 频道的 Release 类型和版本后缀符合仓库约定。",
        '- [ ] 上传 ZIP 与同名 `.sha256` sidecar，并回读远端文件核对 SHA-256。',
        '- [ ] 确认没有把 beta 资产混入 stable 正式 Release。',
        '- [ ] 完成实际客户端安装/更新验收后，再单独执行远端发布步骤。',
        '',
        '## 变更摘要',
        '',
        '- 请在此处补充本次面向用户的中文变更说明。'
    )
    [System.IO.File]::WriteAllText($notesPath, ($notes -join [Environment]::NewLine) + [Environment]::NewLine, $utf8NoBom)

    Write-Output "==> plan: $planPath"
    Write-Output "==> notes: $notesPath"
}
catch {
    Write-Error ("Release Prepare 失败：{0}" -f $_.Exception.Message)
    exit 1
}

exit 0
