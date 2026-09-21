# Build backend sidecar exe for Tauri packaging using PyInstaller.
# Generates a standalone sidecar binary without encryption/obfuscation overhead.
# Run from any directory — the script re-anchors to its own folder.

$ErrorActionPreference = "Stop"
$BackendDir = $PSScriptRoot
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $BackendDir ".."))
$BinaryTarget = Join-Path $BackendDir "..\frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe"
$BinaryTarget = [System.IO.Path]::GetFullPath($BinaryTarget)
$BinariesDir = Split-Path $BinaryTarget -Parent
$SpecFile = Join-Path $BackendDir "liuruyan-backend.spec"
$DistDir = Join-Path $BackendDir "dist"
$BuildDir = Join-Path $BackendDir "build"
$PythonExe = Join-Path $BackendDir "venv\Scripts\python.exe"
$BuiltExe = Join-Path $DistDir "liuruyan-backend.exe"

$StartTime = Get-Date
Write-Host "============================================================"
Write-Host "  柳如烟 后端打包 (PyInstaller 纯净模式)"
Write-Host "============================================================"
Write-Host "[build-backend] Project Root : $ProjectRoot"
Write-Host "[build-backend] Spec File    : $SpecFile"
Write-Host "[build-backend] Target       : $BinaryTarget"
Write-Host ""

if (-not (Test-Path $PythonExe)) {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonExe = "python"
    } else {
        throw "未找到 Python 环境 ($PythonExe)。请先配置虚拟环境。"
    }
}

# 确保前端 binaries 目标目录存在
if (-not (Test-Path $BinariesDir)) {
    New-Item -ItemType Directory -Force -Path $BinariesDir | Out-Null
}

# 若已有运行的 backend 进程，尝试停止以解除文件锁
Get-Process -Name "liuruyan-backend" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# 执行 PyInstaller 打包
Write-Host "[build-backend] 正在执行打包..."
Push-Location $BackendDir
try {
    & $PythonExe -m PyInstaller $SpecFile --distpath $DistDir --workpath $BuildDir --clean -y
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出码: $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

if (-not (Test-Path $BuiltExe)) {
    throw "未找到预期的打包输出文件: $BuiltExe"
}

# 复制至 Tauri externalBin 目标路径
Write-Host "[build-backend] 复制产物到 Tauri binaries 目录..."
Copy-Item $BuiltExe -Destination $BinaryTarget -Force

$Duration = (Get-Date) - $StartTime
$SizeMB = [Math]::Round((Get-Item $BinaryTarget).Length / 1MB, 2)

Write-Host ""
Write-Host "============================================================"
Write-Host "  [OK] 后端打包成功！"
Write-Host "  产物路径 : $BinaryTarget"
Write-Host "  产物大小 : $SizeMB MB"
Write-Host "  打包耗时 : $([int]$Duration.TotalSeconds) 秒"
Write-Host "============================================================"
Write-Host ""
