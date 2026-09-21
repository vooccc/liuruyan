# 柳如烟打包进度监听器
# 在新窗口运行，每 15 秒打印一次当前进度
# 打包主流程结束后用 taskkill 关掉本窗口

$Host.UI.RawUI.WindowTitle = "柳如烟打包进度"

$ErrorActionPreference = "SilentlyContinue"

$nuitkaDir = "./backend\dist_nuitka"
$sidecarExe = "./frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe"
$msiDir = "./release\bundle\msi"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  柳如烟打包进度监听器" -ForegroundColor Cyan
Write-Host "  每 15 秒刷新一次" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$startTime = Get-Date

while ($true) {
    $now = Get-Date
    $elapsed = $now - $startTime
    $elapsedStr = "{0:hh\:mm\:ss}" -f $elapsed

    # Nuitka 编译进度
    $oFiles = Get-ChildItem $nuitkaDir -Filter "*.o" -Recurse -ErrorAction SilentlyContinue
    $oCount = ($oFiles | Measure-Object).Count
    $nuitkaExe = Test-Path "$nuitkaDir\liuruyan-backend.exe"

    # sidecar exe
    $sidecar = Get-Item $sidecarExe -ErrorAction SilentlyContinue
    $sidecarReady = $null -ne $sidecar -and $sidecar.Length -gt 10000000

    # MSI 产物
    $msi = Get-ChildItem "$msiDir\*.msi" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1

    # 活跃编译进程
    $gccCount = (Get-Process gcc -ErrorAction SilentlyContinue | Measure-Object).Count
    $cc1Count = (Get-Process cc1 -ErrorAction SilentlyContinue | Measure-Object).Count
    $rustcCount = (Get-Process rustc -ErrorAction SilentlyContinue | Measure-Object).Count
    $nodeCount = (Get-Process node -ErrorAction SilentlyContinue | Measure-Object).Count

    $time = $now.ToString("HH:mm:ss")
    Write-Host "[$time] 已运行: $elapsedStr" -NoNewline -ForegroundColor Yellow
    Write-Host " | .o: $oCount" -NoNewline
    Write-Host " | nuitka-exe: $(if ($nuitkaExe) { 'YES' } else { '-' })" -NoNewline
    Write-Host " | sidecar: $(if ($sidecarReady) { 'YES' } else { '-' })" -NoNewline
    Write-Host " | msi: $(if ($msi) { $msi.Name } else { '-' })" -NoNewline
    Write-Host ""
    Write-Host "         gcc: $gccCount  cc1: $cc1Count  rustc: $rustcCount  node: $nodeCount" -ForegroundColor DarkGray

    # 阶段判断
    if ($msi -and $msi.LastWriteTime -gt $startTime) {
        Write-Host ""
        Write-Host "  >>> MSI 已生成! <<<" -ForegroundColor Green
        Write-Host "  >>> $msi <<<" -ForegroundColor Green
        Write-Host ""
    } elseif ($sidecarReady -and $rustcCount -gt 0) {
        Write-Host "         [阶段] Tauri Rust 编译中..." -ForegroundColor Cyan
    } elseif ($sidecarReady -and $nodeCount -gt 0) {
        Write-Host "         [阶段] 前端 Vite 构建中..." -ForegroundColor Cyan
    } elseif ($oCount -gt 0 -and -not $nuitkaExe) {
        Write-Host "         [阶段] Nuitka C 编译中... ($oCount 个 .o 文件)" -ForegroundColor Cyan
    } elseif ($nuitkaExe -and -not $sidecarReady) {
        Write-Host "         [阶段] 复制 sidecar exe..." -ForegroundColor Cyan
    }

    Write-Host ""
    Start-Sleep -Seconds 15
}
