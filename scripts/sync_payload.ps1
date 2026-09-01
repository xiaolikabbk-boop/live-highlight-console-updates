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
$DistRoot = Join-Path $RepoRoot "dist"
New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
$ManifestFile = Join-Path $RepoRoot "update-manifest.local.json"
$PackageFile = Join-Path $DistRoot "live-highlight-update.zip"
$Files = Get-ChildItem -LiteralPath $PayloadRoot -File -Recurse | Sort-Object FullName | ForEach-Object {
    $Relative = [IO.Path]::GetRelativePath($PayloadRoot, $_.FullName).Replace('\','/')
    [ordered]@{ path=$Relative; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
}
[ordered]@{ version=(Get-Content -LiteralPath (Join-Path $PayloadRoot 'VERSION') -Raw).Trim(); files=@($Files) } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ManifestFile -Encoding UTF8
if (Test-Path -LiteralPath $PackageFile) { Remove-Item -LiteralPath $PackageFile -Force }
$StageRoot = Join-Path $RepoRoot "_package_stage"
if (Test-Path -LiteralPath $StageRoot) { Remove-Item -LiteralPath $StageRoot -Recurse -Force }
New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
try {
    Copy-Item -LiteralPath $PayloadRoot -Destination (Join-Path $StageRoot 'payload') -Recurse -Force
    Copy-Item -LiteralPath $ManifestFile -Destination (Join-Path $StageRoot 'update-manifest.json') -Force
    Compress-Archive -Path (Join-Path $StageRoot 'payload'),(Join-Path $StageRoot 'update-manifest.json') -DestinationPath $PackageFile -CompressionLevel Optimal
}
finally {
    Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ManifestFile -Force -ErrorAction SilentlyContinue
}
Write-Host "已同步安全更新载荷到 $PayloadRoot" -ForegroundColor Green
