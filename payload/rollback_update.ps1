$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$UpdateRoot = Join-Path $Root "_update"
$BackupRoot = Join-Path $UpdateRoot "backups"

function Assert-SafePath([string]$Path) {
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝访问程序目录以外的路径：$resolved"
    }
}

if (-not (Test-Path -LiteralPath $BackupRoot)) { throw "没有找到可回滚的更新备份。" }
$Backup = Get-ChildItem -LiteralPath $BackupRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $Backup) { throw "没有找到可回滚的更新备份。" }
$StateFile = Join-Path $Backup.FullName "update-state.json"
if (-not (Test-Path -LiteralPath $StateFile)) { throw "最近一次备份缺少回滚清单。" }
$State = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json

Write-Host "将从备份 $($Backup.Name) 回滚。请先关闭直播录制剪辑中控台。" -ForegroundColor Yellow
$Answer = Read-Host "输入 YES 确认回滚"
if ($Answer -ne "YES") { Write-Host "已取消。"; exit 0 }

foreach ($Relative in @($State.added_files)) {
    $Target = Join-Path $Root ([string]$Relative)
    Assert-SafePath $Target
    if (Test-Path -LiteralPath $Target -PathType Leaf) { Remove-Item -LiteralPath $Target -Force }
}
foreach ($Relative in @($State.backed_up_files)) {
    $Source = Join-Path (Join-Path $Backup.FullName "files") ([string]$Relative)
    $Target = Join-Path $Root ([string]$Relative)
    Assert-SafePath $Target
    $Parent = Split-Path -Parent $Target
    if (-not (Test-Path -LiteralPath $Parent)) { New-Item -ItemType Directory -Path $Parent -Force | Out-Null }
    Copy-Item -LiteralPath $Source -Destination $Target -Force
}
Write-Host "已回滚到更新前版本。" -ForegroundColor Green
