$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServiceRoot = Join-Path $Root "highlight_service"
$PythonExe = Join-Path $Root "runtime\python.exe"
$InstallMarker = Join-Path $Root "runtime\.install_complete"
$ErrorLog = Join-Path $Root "startup-error.log"
$Pushed = $false

try {
    if (Test-Path -LiteralPath $ErrorLog) {
        Remove-Item -LiteralPath $ErrorLog -Force
    }

    Write-Host "==============================================="
    Write-Host "  Live Highlight Console"
    Write-Host "==============================================="

    if (-not (Test-Path -LiteralPath $InstallMarker)) {
        Write-Host "First run detected. Downloading the runtime and speech model." -ForegroundColor Cyan
        Write-Host "Please keep this window open. The first setup can take a while." -ForegroundColor Yellow
        & (Join-Path $Root "first_setup.ps1")
        if (-not (Test-Path -LiteralPath $InstallMarker)) {
            throw "First-time setup did not create its completion marker."
        }
    }

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Portable Python is missing. Run first_setup.ps1 again."
    }
    if (-not (Test-Path -LiteralPath $ServiceRoot)) {
        throw "The highlight_service folder is missing. Extract the complete ZIP before starting."
    }

    $EnvFile = Join-Path $ServiceRoot ".env"
    $EnvExample = Join-Path $ServiceRoot ".env.example"
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    }
    $HasApiKey = Select-String -LiteralPath $EnvFile -Pattern '^HIGHLIGHT_AI_API_KEY=\s*\S+' -Quiet
    if (-not $HasApiKey) {
        Write-Host "Enter HIGHLIGHT_AI_API_KEY in the configuration file that opens." -ForegroundColor Yellow
        Start-Process -FilePath "notepad.exe" -ArgumentList ('"' + $EnvFile + '"') -Wait
    }

    $env:HF_HOME = Join-Path $ServiceRoot "data\models"
    Push-Location $ServiceRoot
    $Pushed = $true
    Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
        "-NoLogo", "-NoProfile", "-Command",
        "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8876'"
    )
    Write-Host "Starting the console at http://127.0.0.1:8876 ..." -ForegroundColor Green
    & $PythonExe -m app.main
    $WebUpdateMarker = Join-Path $Root "_update\web-update-in-progress"
    if (Test-Path -LiteralPath $WebUpdateMarker) {
        Write-Host "Web update is restarting the console..." -ForegroundColor Cyan
        exit 0
    }
    if ($LASTEXITCODE -ne 0) {
        throw "The application exited with code $LASTEXITCODE."
    }
}
catch {
    $details = @(
        "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "Message: $($_.Exception.Message)"
        "Type: $($_.Exception.GetType().FullName)"
        "Location: $($_.InvocationInfo.PositionMessage)"
        "Stack: $($_.ScriptStackTrace)"
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $ErrorLog -Value $details -Encoding UTF8
    Write-Host ""
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Details were saved to: $ErrorLog" -ForegroundColor Yellow
    exit 1
}
finally {
    if ($Pushed) { Pop-Location }
}
