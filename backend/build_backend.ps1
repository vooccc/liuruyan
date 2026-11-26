# Build backend directory bundle (--onedir) for Tauri packaging using PyInstaller.
# Generates an unpacked backend-dist directory so cold start skips self-extraction.
# Run from any directory — the script re-anchors to its own folder.

$ErrorActionPreference = "Stop"
$BackendDir = $PSScriptRoot
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $BackendDir ".."))
$DistBundleDir = Join-Path $ProjectRoot "frontend\src-tauri\backend-dist"
$BinaryTarget = Join-Path $DistBundleDir "liuruyan-backend.exe"
$LegacyBinaryTarget = Join-Path $ProjectRoot "frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe"
$SpecFile = Join-Path $BackendDir "liuruyan-backend.spec"
$DistDir = Join-Path $BackendDir "dist"
$BuildDir = Join-Path $BackendDir "build"
$PythonExe = Join-Path $BackendDir "venv\Scripts\python.exe"
$BuiltDir = Join-Path $DistDir "liuruyan-backend"
$BuiltExe = Join-Path $BuiltDir "liuruyan-backend.exe"

$StartTime = Get-Date
Write-Host "============================================================"
Write-Host "  柳如烟 后端打包 (PyInstaller onedir 极速冷启模式)"
Write-Host "============================================================"
Write-Host "[build-backend] Project Root : $ProjectRoot"
Write-Host "[build-backend] Spec File    : $SpecFile"
Write-Host "[build-backend] Target Dir   : $DistBundleDir"
Write-Host ""

if (-not (Test-Path $PythonExe)) {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonExe = "python"
    } else {
        throw "未找到 Python 环境 ($PythonExe)。请先配置虚拟环境。"
    }
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

# 同步整个 onedir 目录到 Tauri resources 目标路径
Write-Host "[build-backend] 同步目录到 Tauri backend-dist..."
if (Test-Path $DistBundleDir) {
    $existingItem = Get-Item $DistBundleDir -Force
    if ($existingItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $existingItem.Delete()
    } else {
        Remove-Item -Recurse -Force $DistBundleDir
    }
}
New-Item -ItemType Directory -Force -Path $DistBundleDir | Out-Null
Copy-Item (Join-Path $BuiltDir "*") -Destination $DistBundleDir -Recurse -Force

# 清理旧版 onefile 遗留产物，避免重复占位或混淆
if (Test-Path $LegacyBinaryTarget) {
    Remove-Item -Force $LegacyBinaryTarget -ErrorAction SilentlyContinue
}

$Duration = (Get-Date) - $StartTime
$TotalBytes = (Get-ChildItem $DistBundleDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
$SizeMB = [Math]::Round($TotalBytes / 1MB, 2)

Write-Host ""
Write-Host "============================================================"
Write-Host "  [OK] 后端打包成功！"
Write-Host "  产物目录 : $DistBundleDir"
Write-Host "  总体积   : $SizeMB MB"
Write-Host "  打包耗时 : $([int]$Duration.TotalSeconds) 秒"
Write-Host "============================================================"
Write-Host ""
