[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Alias('Version')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$')]
    [string]$ReleaseVersion,
    [ValidateSet('beta', 'stable')]
    [string]$Channel,
    [string]$OutputDirectory,
    [string]$PythonEmbedDirectory
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
            throw "发布包缺少 Python embedded 运行时文件：$requiredEntry"
        }
    }
    $wheelCount = @($lowerNames | Where-Object { $_ -match '^third_party/[^/]+\.whl$' }).Count
    if ($wheelCount -lt 1) {
        throw '发布包缺少 third_party/*.whl 依赖文件。'
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

    $packageArguments = @{
        OutputDirectory = $OutputDirectory
        ReleaseVersion = $ReleaseVersion
    }
    if (-not [string]::IsNullOrWhiteSpace($PythonEmbedDirectory)) {
        $packageArguments.PythonEmbedDirectory = $PythonEmbedDirectory
    }
    & $wrapper @packageArguments
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
    Assert-PackageRuntime -ArchivePath $archivePath

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
    if ($Channel -eq 'beta') {
        $channelPosition = 'beta 是测试频道，用于先行验证新版本；遇到问题时请保留旧包并反馈日志。'
    }
    else {
        $channelPosition = 'stable 是默认稳定频道；beta 用于先行验证，稳定用户应优先选择 stable。'
    }
    $notes = @(
        "# ETLP $Channel $ReleaseVersion",
        '',
        '## 版本信息',
        '',
        "- 频道：$Channel",
        "- 版本：$ReleaseVersion",
        "- 分支：$branch",
        ('- Commit：`{0}`' -f $commit),
        ('- ZIP：`{0}`（{1} MiB，{2} bytes）' -f $packageAsset, $sizeMiB, $archiveInfo.Length),
        ('- SHA-256：`{0}`' -f $archiveHash),
        ('- SHA-256 文件：`{0}`' -f $checksumAsset),
        '',
        '## 包内内容',
        '',
        '- ZIP 自带 Windows Python 3.9 x86 embedded runtime，用户不需要另行安装 Python。',
        '- `third_party/*.whl` 随包提供项目依赖；首次启动只从包内 wheel 在本地准备依赖，不会在运行时联网下载依赖。',
        '- 根目录提供 `embyToLocalPlayer_debug.bat`，可启动服务或执行频道更新。',
        '',
        '## 安装与更新',
        '',
        "1. 下载 `$packageAsset` 和 `$checksumAsset`，使用 SHA-256 校验文件确认 ZIP 完整。",
        '2. 将 ZIP 解压到新的英文路径；更新已有安装时替换程序文件，并保留自己的配置备份。',
        '3. 运行根目录 `embyToLocalPlayer_debug.bat`，选择 `1` 在当前窗口启动；选择 `6` 检查并安装当前频道更新。',
        '4. 更新器会先验证 SHA-256 和包内发布清单，验证成功后才替换旧包。',
        '',
        '## 频道定位',
        '',
        "- $channelPosition",
        '',
        '## 已知限制',
        '',
        '- 自带运行时面向 Windows 32 位 Python 3.9；本 ZIP 不作为 macOS/Linux 安装包。',
        '- 更新功能需要访问 GitHub Release；播放功能仍依赖 Emby/Jellyfin、播放器和本机路径映射配置。',
        '- beta 版本优先用于测试，可能包含尚未在所有播放器和服务器组合中验证的改动。'
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
