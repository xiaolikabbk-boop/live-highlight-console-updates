param([switch]$NoDesktopWindow)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServiceRoot = Join-Path $Root "highlight_service"
$PythonExe = Join-Path $Root "runtime\python.exe"
$InstallMarker = Join-Path $Root "runtime\.install_complete"
$ErrorLog = Join-Path $Root "startup-error.log"
$RestartMarker = Join-Path $Root "_workbench-restart-requested"
$DesktopLauncher = Join-Path $ServiceRoot "app\LiveHighlightWorkbench.exe"
$Address = "http://127.0.0.1:8876/"
$Pushed = $false

function Test-WorkbenchReady {
    try {
        $request = [Net.WebRequest]::Create($Address)
        $request.Timeout = 900
        $response = $request.GetResponse()
        $response.Close()
        return $true
    }
    catch { return $false }
}

function Find-EdgeExecutable {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe")
    )
    return $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

function Open-DesktopWindow {
    $edge = Find-EdgeExecutable
    if ($edge) {
        Start-Process -FilePath $edge -ArgumentList @("--app=$Address", "--start-maximized", "--no-first-run")
    }
    else { Start-Process $Address }
}

function Ensure-DesktopShortcut {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        if (-not $desktop) { return }
        $shortcutPath = Join-Path $desktop "直播高光工作台.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $DesktopLauncher
        $shortcut.Arguments = ""
        $shortcut.WorkingDirectory = $Root
        $edge = Find-EdgeExecutable
        if ($edge) { $shortcut.IconLocation = "$edge,0" }
        $shortcut.Description = "直播高光录制、转写、筛选与成片工作台"
        $shortcut.Save()
    }
    catch { }
}

try {
    if (Test-Path -LiteralPath $ErrorLog) { Remove-Item -LiteralPath $ErrorLog -Force }
    Ensure-DesktopShortcut

    # Repeated double-clicks only reopen the desktop window. They never start a
    # second backend, recorder, or worker queue.
    if (Test-WorkbenchReady) {
        if (-not $NoDesktopWindow) { Open-DesktopWindow }
        exit 0
    }

    if (-not (Test-Path -LiteralPath $InstallMarker)) {
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
    if (-not (Test-Path -LiteralPath $EnvFile)) { Copy-Item -LiteralPath $EnvExample -Destination $EnvFile }

    # Install the official CUDA 12/cuDNN 9 runtime once when CUDA is requested.
    $CudaRequested = Select-String -LiteralPath $EnvFile -Pattern '^HIGHLIGHT_WHISPER_DEVICE=\s*cuda\s*$' -Quiet
    $NvidiaSmi = Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue
    $SitePackages = Join-Path $Root "runtime\Lib\site-packages"
    $CublasBin = Join-Path $SitePackages "nvidia\cublas\bin"
    $CudnnBin = Join-Path $SitePackages "nvidia\cudnn\bin"
    $CublasDll = Join-Path $CublasBin "cublas64_12.dll"
    $CudnnDll = Join-Path $CudnnBin "cudnn_ops64_9.dll"
    if ($CudaRequested -and $NvidiaSmi -and
        (-not (Test-Path -LiteralPath $CublasDll) -or -not (Test-Path -LiteralPath $CudnnDll))) {
        & $PythonExe -m pip install --disable-pip-version-check --upgrade --progress-bar on nvidia-cublas-cu12 nvidia-cudnn-cu12
    }
    if (Test-Path -LiteralPath $CublasBin) { $env:PATH = "$CublasBin;$env:PATH" }
    if (Test-Path -LiteralPath $CudnnBin) { $env:PATH = "$CudnnBin;$env:PATH" }

    $ActiveDataRoot = if ($env:HIGHLIGHT_DATA_DIR) { $env:HIGHLIGHT_DATA_DIR } else { Join-Path $ServiceRoot "data" }
    $env:HF_HOME = Join-Path $ActiveDataRoot "models"
    Push-Location $ServiceRoot
    $Pushed = $true

    do {
        if (Test-Path -LiteralPath $RestartMarker) { Remove-Item -LiteralPath $RestartMarker -Force }
        & $PythonExe -m app.main
        if (Test-Path -LiteralPath $RestartMarker) {
            Remove-Item -LiteralPath $RestartMarker -Force
            Start-Sleep -Seconds 1
            continue
        }
        break
    } while ($true)

    $WebUpdateMarker = Join-Path $Root "_update\web-update-in-progress"
    if (Test-Path -LiteralPath $WebUpdateMarker) { exit 0 }
    if ($LASTEXITCODE -ne 0) { throw "The application exited with code $LASTEXITCODE." }
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
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shell.Popup(
            "直播高光工作台启动失败。`n`n$($_.Exception.Message)`n`n诊断信息已保存到：$ErrorLog",
            0, "直播高光工作台", 16
        ) | Out-Null
    } catch { }
    exit 1
}
finally {
    if ($Pushed) { Pop-Location }
}
