$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$WebInstall = $args.Count -ge 1 -and [string]$args[0] -eq '-WebInstall'
$ServicePid = if ($WebInstall -and $args.Count -ge 2 -and [string]$args[1] -match '^\d+$') { [int]$args[1] } else { 0 }
$VersionFile = Join-Path $Root "VERSION"
$ConfigFile = Join-Path $Root "update-config.json"
$UpdateRoot = Join-Path $Root "_update"
$BackupRoot = Join-Path $UpdateRoot "backups"
$LogRoot = Join-Path $UpdateRoot "logs"
$PidFile = Join-Path $Root "highlight_service\data\service.pid"
$WebMarker = Join-Path $UpdateRoot "web-update-in-progress"
$WebStatus = Join-Path $UpdateRoot "web-update-status.json"
$ServiceStopped = $false
$WorkDir = $null
$BackupDir = $null
$State = $null

function Set-WebUpdateStatus([string]$Status, [string]$Message, [string]$Version = "") {
    if (-not $WebInstall) { return }
    New-Item -ItemType Directory -Path $UpdateRoot -Force | Out-Null
    [ordered]@{ status=$Status; message=$Message; version=$Version; updated_at=(Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $WebStatus -Encoding UTF8
}

function Convert-Version([string]$Value) {
    try { return [version]($Value.Trim().TrimStart('v','V')) }
    catch { return [version]"0.0.0.0" }
}

function Normalize-RelativePath([string]$Value) {
    $Path = $Value.Replace('/', '\').TrimStart('\')
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.Contains('..') -or [IO.Path]::IsPathRooted($Path) -or $Path.Contains(':')) {
        throw "更新清单包含不安全路径：$Value"
    }
    return $Path
}

function Test-AllowedUpdatePath([string]$Relative) {
    if ($Relative.StartsWith('highlight_service\app\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    $AllowedRootFiles = @(
        'VERSION', 'start_console.ps1', '启动直播录制剪辑中控台.bat', '使用说明.txt',
        '检查并安装更新.bat', '回滚上一个版本.bat',
        'update.ps1', 'rollback_update.ps1'
    )
    return $AllowedRootFiles -contains $Relative
}

function Assert-UnderRoot([string]$Path, [string]$Base) {
    $resolvedBase = [IO.Path]::GetFullPath($Base).TrimEnd('\') + '\'
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($resolvedBase, [StringComparison]::OrdinalIgnoreCase)) {
        throw "路径越界：$resolved"
    }
}

function Restore-Backup($State, [string]$BackupDir) {
    foreach ($Relative in @($State.added_files)) {
        $Target = Join-Path $Root ([string]$Relative)
        Assert-UnderRoot $Target $Root
        if (Test-Path -LiteralPath $Target -PathType Leaf) { Remove-Item -LiteralPath $Target -Force }
    }
    foreach ($Relative in @($State.backed_up_files)) {
        $Source = Join-Path (Join-Path $BackupDir 'files') ([string]$Relative)
        $Target = Join-Path $Root ([string]$Relative)
        Assert-UnderRoot $Target $Root
        $Parent = Split-Path -Parent $Target
        if (-not (Test-Path -LiteralPath $Parent)) { New-Item -ItemType Directory -Path $Parent -Force | Out-Null }
        Copy-Item -LiteralPath $Source -Destination $Target -Force
    }
}

Write-Host "==============================================="
Write-Host "  直播录制剪辑中控台 - 安全更新"
Write-Host "==============================================="
if (-not (Test-Path -LiteralPath $ConfigFile)) { throw "缺少 update-config.json。" }
$Config = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
$Repository = [string]$Config.repository
$AssetName = [string]$Config.asset_name
$CurrentText = if (Test-Path -LiteralPath $VersionFile) { (Get-Content -LiteralPath $VersionFile -Raw).Trim() } else { "0.0.0.0" }
Write-Host "当前版本：$CurrentText" -ForegroundColor Cyan

if (Test-Path -LiteralPath $PidFile) {
    $PidText = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($PidText -match '^\d+$' -and (Get-Process -Id ([int]$PidText) -ErrorAction SilentlyContinue)) {
        if (-not $WebInstall -or [int]$PidText -ne $ServicePid) {
            Write-Host "检测到中控台仍在运行。为避免打断转写或渲染，请先关闭黑色中控台窗口，再重新运行本更新程序。" -ForegroundColor Yellow
            exit 2
        }
    }
}

$Headers = @{ 'Accept'='application/vnd.github+json'; 'User-Agent'='Live-Highlight-Updater'; 'X-GitHub-Api-Version'='2022-11-28' }
$ReleaseUri = "https://api.github.com/repos/$Repository/releases/latest"
$AssetUris = @()
try {
    $Release = Invoke-RestMethod -Uri $ReleaseUri -Headers $Headers -Method Get -TimeoutSec 30
    $AvailableText = ([string]$Release.tag_name).TrimStart('v','V')
    $Asset = @($Release.assets) | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
    if ($null -eq $Asset) { throw "最新版本没有找到更新包 $AssetName。" }
    $AssetUris = @([string]$Asset.browser_download_url)
}
catch {
    Write-Host "GitHub API 暂时不可用，正在切换公开直链备用线路……" -ForegroundColor Yellow
    $RawVersionUri = "https://raw.githubusercontent.com/$Repository/main/payload/VERSION"
    try {
        $AvailableText = ([string](Invoke-RestMethod -Uri $RawVersionUri -Headers @{ 'User-Agent'='Live-Highlight-Updater' } -Method Get -TimeoutSec 30)).Trim().TrimStart('v','V')
        $AssetUris = @("https://github.com/$Repository/releases/latest/download/$AssetName")
    }
    catch {
        Write-Host "GitHub 公开直链也不可用，正在切换 jsDelivr CDN……" -ForegroundColor Yellow
        $CdnVersionUri = "https://cdn.jsdelivr.net/gh/${Repository}@main/payload/VERSION"
        try {
            $AvailableText = ([string](Invoke-RestMethod -Uri $CdnVersionUri -Headers @{ 'User-Agent'='Live-Highlight-Updater' } -Method Get -TimeoutSec 30)).Trim().TrimStart('v','V')
        }
        catch { throw "GitHub 与 CDN 更新线路均无法访问。请使用离线更新补丁。$($_.Exception.Message)" }
    }
}
$CdnAssetUri = "https://cdn.jsdelivr.net/gh/${Repository}@v${AvailableText}/dist/$AssetName"
if ($AssetUris -notcontains $CdnAssetUri) { $AssetUris += $CdnAssetUri }
Write-Host "可用版本：$AvailableText" -ForegroundColor Cyan
if ((Convert-Version $AvailableText) -le (Convert-Version $CurrentText)) {
    Write-Host "当前已经是最新版本，无需更新。" -ForegroundColor Green
    Set-WebUpdateStatus "current" "当前已经是最新版本" $CurrentText
    exit 0
}
$Answer = if ($WebInstall) { "YES" } else { Read-Host "发现新版本。输入 YES 下载、备份并安装" }
if ($Answer -ne 'YES') { Write-Host "已取消更新。"; exit 0 }

New-Item -ItemType Directory -Path $UpdateRoot,$BackupRoot,$LogRoot -Force | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$WorkDir = Join-Path $UpdateRoot ("work_" + $Stamp)
$ArchiveFile = Join-Path $WorkDir $AssetName
$ExtractDir = Join-Path $WorkDir "extracted"
$BackupDir = Join-Path $BackupRoot ($CurrentText + '_before_' + $AvailableText + '_' + $Stamp)
New-Item -ItemType Directory -Path $WorkDir,$ExtractDir,(Join-Path $BackupDir 'files') -Force | Out-Null
Assert-UnderRoot $WorkDir $UpdateRoot

try {
    Write-Host "正在下载更新包……"
    $DownloadError = $null
    foreach ($AssetUri in $AssetUris) {
        try {
            if (Test-Path -LiteralPath $ArchiveFile) { Remove-Item -LiteralPath $ArchiveFile -Force }
            Invoke-WebRequest -Uri $AssetUri -Headers @{ 'User-Agent'='Live-Highlight-Updater' } -OutFile $ArchiveFile -UseBasicParsing -TimeoutSec 180
            $DownloadError = $null
            break
        }
        catch {
            $DownloadError = $_.Exception
            Write-Host "当前下载线路失败，正在尝试下一条……" -ForegroundColor Yellow
        }
    }
    if ($null -ne $DownloadError -or -not (Test-Path -LiteralPath $ArchiveFile)) {
        throw "所有在线下载线路均失败，请使用离线更新补丁。$($DownloadError.Message)"
    }
    Expand-Archive -LiteralPath $ArchiveFile -DestinationPath $ExtractDir -Force
    $ManifestFile = Join-Path $ExtractDir 'update-manifest.json'
    $PayloadRoot = Join-Path $ExtractDir 'payload'
    if (-not (Test-Path -LiteralPath $ManifestFile) -or -not (Test-Path -LiteralPath $PayloadRoot)) { throw "更新包结构不完整。" }
    $Manifest = Get-Content -LiteralPath $ManifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$Manifest.version -ne $AvailableText) { throw "更新包版本与 GitHub 发布版本不一致。" }

    $State = [ordered]@{ from_version=$CurrentText; to_version=$AvailableText; created_at=(Get-Date).ToString('o'); backed_up_files=@(); added_files=@() }
    $Validated = @()
    foreach ($File in @($Manifest.files)) {
        $Relative = Normalize-RelativePath ([string]$File.path)
        if (-not (Test-AllowedUpdatePath $Relative)) { throw "更新包试图修改受保护内容：$Relative" }
        $Source = Join-Path $PayloadRoot $Relative
        Assert-UnderRoot $Source $PayloadRoot
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "更新包缺少文件：$Relative" }
        $ActualHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualHash -ne ([string]$File.sha256).ToLowerInvariant()) { throw "文件校验失败：$Relative" }
        $Validated += [pscustomobject]@{ Relative=$Relative; Source=$Source }
    }
    if ($Validated.Count -eq 0) { throw "更新包没有可安装文件。" }

    foreach ($Item in $Validated) {
        $Target = Join-Path $Root $Item.Relative
        Assert-UnderRoot $Target $Root
        if (Test-Path -LiteralPath $Target -PathType Leaf) {
            $BackupFile = Join-Path (Join-Path $BackupDir 'files') $Item.Relative
            $BackupParent = Split-Path -Parent $BackupFile
            if (-not (Test-Path -LiteralPath $BackupParent)) { New-Item -ItemType Directory -Path $BackupParent -Force | Out-Null }
            Copy-Item -LiteralPath $Target -Destination $BackupFile -Force
            $State.backed_up_files += $Item.Relative
        } else { $State.added_files += $Item.Relative }
    }
    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $BackupDir 'update-state.json') -Encoding UTF8
    if ($WebInstall -and $ServicePid -gt 0) {
        New-Item -ItemType File -Path $WebMarker -Force | Out-Null
        Set-WebUpdateStatus "installing" "更新包已校验，正在重启中控台" $AvailableText
        Stop-Process -Id $ServicePid -Force -ErrorAction SilentlyContinue
        $ServiceStopped = $true
        Start-Sleep -Seconds 2
    }
    foreach ($Item in $Validated) {
        $Target = Join-Path $Root $Item.Relative
        $TargetParent = Split-Path -Parent $Target
        if (-not (Test-Path -LiteralPath $TargetParent)) { New-Item -ItemType Directory -Path $TargetParent -Force | Out-Null }
        Copy-Item -LiteralPath $Item.Source -Destination $Target -Force
    }
    Write-Host "更新完成：$CurrentText -> $AvailableText" -ForegroundColor Green
    Write-Host "直播间、密钥、数据库、录像、素材、模型和导出记录均未触碰。" -ForegroundColor Green
    Set-WebUpdateStatus "complete" "更新完成，正在重新启动中控台" $AvailableText
}
catch {
    if ($null -ne $State -and (Test-Path -LiteralPath $BackupDir)) {
        Write-Host "安装失败，正在自动恢复更新前版本……" -ForegroundColor Yellow
        Restore-Backup $State $BackupDir
    }
    Set-WebUpdateStatus "failed" $_.Exception.Message $CurrentText
    if ($WebInstall -and $ServiceStopped) {
        Remove-Item -LiteralPath $WebMarker -Force -ErrorAction SilentlyContinue
        Start-Process -FilePath (Join-Path $Root '启动直播录制剪辑中控台.bat') -WorkingDirectory $Root
    }
    throw
}
finally {
    if ($WorkDir -and (Test-Path -LiteralPath $WorkDir)) { Remove-Item -LiteralPath $WorkDir -Recurse -Force }
}
if ($WebInstall) {
    Remove-Item -LiteralPath $WebMarker -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath (Join-Path $Root '启动直播录制剪辑中控台.bat') -WorkingDirectory $Root
}
