$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$PayloadRoot = Join-Path $RepoRoot "payload"
$ExpectedPayload = [IO.Path]::GetFullPath((Join-Path $RepoRoot "payload"))
if ([IO.Path]::GetFullPath($PayloadRoot) -ne $ExpectedPayload) { throw "发布目录校验失败。" }
if (Test-Path -LiteralPath $PayloadRoot) { Remove-Item -LiteralPath $PayloadRoot -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $PayloadRoot "highlight_service\app") -Force | Out-Null

$AppSource = Join-Path $WorkspaceRoot "highlight_service\app"
Get-ChildItem -LiteralPath $AppSource -Force | Where-Object { $_.Name -ne "__pycache__" } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $PayloadRoot "highlight_service\app") -Recurse -Force
}

$PortableSource = Join-Path $WorkspaceRoot "highlight_service\portable"
$RootFiles = @(
    "VERSION", "start_console.ps1", "启动直播录制剪辑中控台.bat", "使用说明.txt",
    "检查并安装更新.bat", "回滚上一个版本.bat", "update.ps1", "rollback_update.ps1"
)
foreach ($Name in $RootFiles) {
    $Source = Join-Path $PortableSource $Name
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "缺少发布文件：$Source" }
    Copy-Item -LiteralPath $Source -Destination (Join-Path $PayloadRoot $Name) -Force
}
Write-Host "已同步安全更新载荷到 $PayloadRoot" -ForegroundColor Green
