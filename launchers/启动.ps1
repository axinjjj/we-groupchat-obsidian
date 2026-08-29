$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $ProjectDir ".venv"
$PythonBin = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsFile = Join-Path $ProjectDir "requirements.txt"
$RequirementsStamp = Join-Path $VenvDir ".requirements.sha256"
$env:WE_GROUPCHAT_OBSIDIAN_DATA_DIR = Join-Path $env:USERPROFILE ".we-groupchat-obsidian"

$SetupOnly = $args -contains "--setup-only"
$HealthCheck = $args -contains "--health-check"
$RefreshDataSource = $args -contains "--refresh-data-source"
$InstallAutostart = $args -contains "--install-autostart"
$UninstallAutostart = $args -contains "--uninstall-autostart"
$AutostartStatus = $args -contains "--autostart-status"
$Autostart = $args -contains "--autostart"
$NoPause = $Autostart -or ($args -contains "--no-pause") -or ($env:WE_GROUPCHAT_OBSIDIAN_NO_PAUSE -eq "1")
$AssumeYes = $args -contains "--yes"

function Pause-Wgo([int] $ExitCode) {
    if (-not $NoPause) {
        [void](Read-Host "按回车键关闭")
    }
    exit $ExitCode
}

function Invoke-BasePython([string[]] $PythonArgs) {
    $PyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $PyLauncher) {
        & $PyLauncher.Source -3 @PythonArgs
        return $LASTEXITCODE
    }
    $Python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -eq $Python) {
        throw "未找到 Python 3。请从 python.org 安装 Python 3.10 或更高版本。"
    }
    & $Python.Source @PythonArgs
    return $LASTEXITCODE
}

function Confirm-DependencyInstall {
    if ($AssumeYes) {
        return $true
    }
    $Answer = Read-Host "创建/更新项目 .venv 并安装 requirements.txt 中的依赖？[y/N]"
    return $Answer -eq "y" -or $Answer -eq "Y"
}

function Get-WgoSha256([string] $Path) {
    $Stream = [System.IO.File]::OpenRead($Path)
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = $Sha256.ComputeHash($Stream)
        return ([System.BitConverter]::ToString($Bytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $Sha256.Dispose()
        $Stream.Dispose()
    }
}

try {
    Set-Location -LiteralPath $ProjectDir
    if ($UninstallAutostart -and -not (Test-Path -LiteralPath $PythonBin)) {
        $Code = Invoke-BasePython @((Join-Path $ProjectDir "scripts\windows_autostart.py"), "uninstall")
        Pause-Wgo $Code
    }
    $CurrentHash = Get-WgoSha256 $RequirementsFile
    $InstalledHash = ""
    if (Test-Path -LiteralPath $RequirementsStamp) {
        $InstalledHash = (Get-Content -Raw -LiteralPath $RequirementsStamp).Trim()
    }
    $NeedsInstall = (-not (Test-Path -LiteralPath $PythonBin)) -or ($CurrentHash -ne $InstalledHash)
    if ($NeedsInstall -and $Autostart) {
        throw "项目依赖已变化；请先手动运行 启动.cmd 完成更新，再恢复登录自启。"
    }
    if ($NeedsInstall -and -not (Confirm-DependencyInstall)) {
        Write-Host "已取消依赖安装，程序不会启动。"
        Pause-Wgo 1
    }
    if (-not (Test-Path -LiteralPath $PythonBin)) {
        Write-Host "[1/3] 创建项目隔离环境..."
        $Code = Invoke-BasePython @("-m", "venv", $VenvDir)
        if ($Code -ne 0) {
            throw "创建 .venv 失败。"
        }
    }
    if ($CurrentHash -ne $InstalledHash) {
        Write-Host "[2/3] 安装项目依赖（首次可能需要几分钟）..."
        & $PythonBin -m pip install -r $RequirementsFile
        if ($LASTEXITCODE -ne 0) {
            throw "安装 requirements.txt 失败。"
        }
        Set-Content -LiteralPath $RequirementsStamp -Value $CurrentHash -Encoding ascii -NoNewline
    } else {
        Write-Host "[2/3] Python 依赖已就绪"
    }

    if ($InstallAutostart) {
        Write-Host "登录自启需要自动续期数据源；raw key 将遮蔽输入并仅保存到 Windows 凭据管理器。"
        & $PythonBin (Join-Path $ProjectDir "scripts\refresh_data_source.py") --raw-key --remember-raw-key
        if ($LASTEXITCODE -ne 0) {
            Pause-Wgo $LASTEXITCODE
        }
        & $PythonBin (Join-Path $ProjectDir "scripts\windows_autostart.py") install
        $InstallCode = $LASTEXITCODE
        if ($InstallCode -ne 0) {
            & $PythonBin (Join-Path $ProjectDir "scripts\refresh_data_source.py") --forget-raw-key
        }
        Pause-Wgo $InstallCode
    }
    if ($UninstallAutostart) {
        & $PythonBin (Join-Path $ProjectDir "scripts\windows_autostart.py") uninstall
        Pause-Wgo $LASTEXITCODE
    }
    if ($AutostartStatus) {
        & $PythonBin (Join-Path $ProjectDir "scripts\windows_autostart.py") status
        Pause-Wgo $LASTEXITCODE
    }

    Write-Host "[3/3] 检查 Windows 微信数据源..."
    & $PythonBin (Join-Path $ProjectDir "scripts\windows_setup.py")
    $Readiness = $LASTEXITCODE

    if ($SetupOnly -or $HealthCheck) {
        Pause-Wgo $Readiness
    }
    if ($RefreshDataSource) {
        & $PythonBin (Join-Path $ProjectDir "scripts\refresh_data_source.py") --raw-key
        Pause-Wgo $LASTEXITCODE
    }
    if ($Readiness -eq 3) {
        Pause-Wgo 3
    }
    if ($Readiness -eq 2) {
        & $PythonBin (Join-Path $ProjectDir "scripts\refresh_data_source.py") --stored-raw-key
        $RefreshCode = $LASTEXITCODE
        if ($RefreshCode -ne 0 -and -not $Autostart) {
            & $PythonBin (Join-Path $ProjectDir "scripts\refresh_data_source.py") --raw-key
            $RefreshCode = $LASTEXITCODE
        }
        if ($RefreshCode -ne 0) {
            Pause-Wgo $RefreshCode
        }
    }

    Write-Host "正在启动微信总结；托盘区会出现应用图标。"
    & $PythonBin (Join-Path $ProjectDir "app.py")
    Pause-Wgo $LASTEXITCODE
} catch {
    Write-Host "❌ $($_.Exception.Message)"
    Pause-Wgo 1
}
