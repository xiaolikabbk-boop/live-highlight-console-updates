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

    # The Windows CTranslate2 wheel uses CUDA 12 plus NVIDIA's cuBLAS/cuDNN 9
    # DLLs.  Install the official Python-packaged runtime once on CUDA systems.
    # Existing data, models and queued jobs are not touched.
    $CudaRequested = Select-String -LiteralPath $EnvFile -Pattern '^HIGHLIGHT_WHISPER_DEVICE=\s*cuda\s*$' -Quiet
    $NvidiaSmi = Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue
    $SitePackages = Join-Path $Root "runtime\Lib\site-packages"
    $CublasBin = Join-Path $SitePackages "nvidia\cublas\bin"
    $CudnnBin = Join-Path $SitePackages "nvidia\cudnn\bin"
    $CublasDll = Join-Path $CublasBin "cublas64_12.dll"
    $CudnnDll = Join-Path $CudnnBin "cudnn_ops64_9.dll"
    if ($CudaRequested -and $NvidiaSmi -and
        (-not (Test-Path -LiteralPath $CublasDll) -or -not (Test-Path -LiteralPath $CudnnDll))) {
        Write-Host ""
        Write-Host "NVIDIA GPU detected. Installing the GPU transcription runtime..." -ForegroundColor Cyan
        Write-Host "This one-time download is large and can take 5-20 minutes." -ForegroundColor Yellow
        Write-Host "Download progress will appear below. Please keep this window open." -ForegroundColor Yellow
        & $PythonExe -m pip install --disable-pip-version-check --upgrade --progress-bar on nvidia-cublas-cu12 nvidia-cudnn-cu12
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $CublasDll) -and (Test-Path -LiteralPath $CudnnDll)) {
            Write-Host "GPU transcription runtime installed successfully." -ForegroundColor Green
        }
        else {
            Write-Host "GPU runtime installation did not complete. The console will continue with CPU fallback." -ForegroundColor Yellow
            Write-Host "Restart the console later to retry the installation." -ForegroundColor Yellow
        }
        Write-Host ""
    }
    if (Test-Path -LiteralPath $CublasBin) { $env:PATH = "$CublasBin;$env:PATH" }
    if (Test-Path -LiteralPath $CudnnBin) { $env:PATH = "$CudnnBin;$env:PATH" }

    $env:HF_HOME = Join-Path $ServiceRoot "data\models"
    Push-Location $ServiceRoot
    $Pushed = $true
    # Do not open a browser on a fixed timer: on a cold start the server can
    # take longer than three seconds, which leaves the user on a misleading
    # "connection refused" page.  Wait until the local server answers first.
    $BrowserWaitCommand = @'
$ErrorActionPreference = 'SilentlyContinue'
$address = 'http://127.0.0.1:8876/'
for ($attempt = 0; $attempt -lt 45; $attempt++) {
    try {
        $request = [Net.WebRequest]::Create($address)
        $request.Timeout = 1000
        $response = $request.GetResponse()
        $response.Close()
        Start-Process $address
        exit 0
    }
    catch { Start-Sleep -Seconds 1 }
}
Start-Process $address
'@.Trim()
    Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
        "-NoLogo", "-NoProfile", "-Command", $BrowserWaitCommand
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
