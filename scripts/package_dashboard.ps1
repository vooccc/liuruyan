param(
    [Parameter(Mandatory=$true)]
    [string]$ProjectRoot,

    [string]$PackageCommand = "npm run package",

    [string]$PackageScript = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $ProjectRoot.Trim().Trim('"')
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$FrontendDir = Join-Path $ProjectRoot "frontend"
$LogPath = Join-Path $ProjectRoot "package_build.log"
$SidecarExe = Join-Path $ProjectRoot "frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe"
$TargetDir = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path (Split-Path $ProjectRoot -Parent) "lry-target" }
$MsiDir = Join-Path $TargetDir "release\bundle\msi"

if ($PackageScript.Trim().Length -gt 0) {
    $PackageCommand = "npm run $PackageScript"
}

$isWritable = $false
try {
    $fs = [System.IO.File]::Open($LogPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    $fs.Close()
    $isWritable = $true
} catch {
    $isWritable = $false
}

$process = $null
if (-not $isWritable) {
    # 检查是否已有打包编译任务正在进行
    $existing = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match 'nuitka|build_backend\.ps1|tauri build|package:lite' -and $_.ProcessId -ne $PID
    }
    if ($existing) {
        Write-Host "[WARN] 检测到已有打包任务正在运行 (PID: $($existing.ProcessId -join ', '))，正在挂接至当前进度看板..."
        $process = Get-Process -Id $existing[0].ProcessId -ErrorAction SilentlyContinue
    } else {
        $env:NODE_OPTIONS = "--max-old-space-size=4096"
        $LogPath = Join-Path $ProjectRoot ("package_build_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
        $process = Start-Process -FilePath "cmd.exe" -ArgumentList "/d", "/c", "set NODE_OPTIONS=--max-old-space-size=4096 && $PackageCommand > `"$LogPath`" 2>&1" -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru
    }
} else {
    $startTime = Get-Date
    $env:NODE_OPTIONS = "--max-old-space-size=4096"
    $process = Start-Process -FilePath "cmd.exe" -ArgumentList "/d", "/c", "set NODE_OPTIONS=--max-old-space-size=4096 && $PackageCommand > `"$LogPath`" 2>&1" -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru
}

function Format-FileSize([long]$bytes) {
    if ($bytes -le 0) { return "-" }
    if ($bytes -gt 1GB) { return ("{0:N2} GB" -f ($bytes / 1GB)) }
    if ($bytes -gt 1MB) { return ("{0:N2} MB" -f ($bytes / 1MB)) }
    if ($bytes -gt 1KB) { return ("{0:N2} KB" -f ($bytes / 1KB)) }
    return ("$bytes B")
}

function Get-StatusLines {
    $now = Get-Date
    $elapsed = $now - $startTime
    $elapsedStr = "{0:hh\:mm\:ss}" -f $elapsed

    $pyinstallerExe = Test-Path (Join-Path $ProjectRoot "backend\dist\liuruyan-backend.exe")

    $sidecar = Get-Item $SidecarExe -ErrorAction SilentlyContinue
    $sidecarSize = if ($null -ne $sidecar) { Format-FileSize $sidecar.Length } else { "-" }
    $sidecarReady = $null -ne $sidecar -and $sidecar.Length -gt 10000000

    $bundleDir = Join-Path $TargetDir "release\bundle"
    $installer = Get-ChildItem -Path $bundleDir -Recurse -File -Include *.exe, *.msi -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match 'bundle\\(nsis|msi)\\' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $installerText = if ($installer -and $installer.LastWriteTime -gt $startTime) { $installer.Name } else { "-" }

    $pythonCount = (Get-Process python -ErrorAction SilentlyContinue | Measure-Object).Count
    $rustcCount = (Get-Process rustc -ErrorAction SilentlyContinue | Measure-Object).Count
    $nodeCount = (Get-Process node -ErrorAction SilentlyContinue | Measure-Object).Count

    $stage = "starting"
    if ($installer -and $installer.LastWriteTime -gt $startTime) {
        $stage = "installer generated"
    } elseif ($sidecarReady -and $rustcCount -gt 0) {
        $stage = "tauri rust build"
    } elseif ($sidecarReady -and $nodeCount -gt 0) {
        $stage = "frontend build / tauri prepare"
    } elseif ($pythonCount -gt 0 -and -not $sidecarReady) {
        $stage = "backend packaging (pyinstaller)"
    } elseif ($nodeCount -gt 0) {
        $stage = "npm / node running"
    }

    @(
        "============================================================",
        "  Liuruyan packaging dashboard",
        "============================================================",
        "Project : $ProjectRoot",
        "Command : $PackageCommand",
        "Elapsed : $elapsedStr",
        "Stage   : $stage",
        "",
        "Backend : $(if ($sidecarReady) { 'READY' } elseif ($pyinstallerExe) { 'DIST BUILT' } else { 'BUILDING' })",
        "Sidecar : $(if ($sidecarReady) { 'READY' } else { '-' }) ($sidecarSize)",
        "Package : $installerText",
        "Process : python=$pythonCount rustc=$rustcCount node=$nodeCount",
        "Log     : $LogPath"
    )
}

function Get-LogTailLines {
    $lines = @()
    $lines += "---- latest build log ----"
    if (Test-Path $LogPath) {
        $lines += Get-Content $LogPath -Tail 16 -ErrorAction SilentlyContinue
    } else {
        $lines += "waiting for build log..."
    }
    return $lines
}

function Write-Dashboard {
    $lines = @()
    $lines += ""
    $lines += "[$((Get-Date).ToString('HH:mm:ss'))] package progress snapshot"
    $lines += Get-StatusLines
    $lines += Get-LogTailLines
    $lines | ForEach-Object { Write-Host $_ }
}

while (-not $process.HasExited) {
    Write-Dashboard
    Start-Sleep -Seconds 5
    $process.Refresh()
}

$process.WaitForExit()
$exitCode = $process.ExitCode

Write-Dashboard
Write-Host ""

exit $exitCode
