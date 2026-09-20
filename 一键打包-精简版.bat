@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion

:: ============================================================
::   Liuruyan RC Package Builder
:: ============================================================

title 柳如烟 一键打包（RC 版）
cd /d "%~dp0"

echo ============================================================
echo   Liuruyan RC Package (companions + council)
echo   Supports Current User / All Users install mode
echo   Package output uses an isolated target dir
echo ============================================================
echo 项目根: %~dp0
echo 开始时间: %date% %time%
echo.

rem --- 打包前先 bump patch：本次直接使用新版本号 ---
echo --- 版本号自动递增 ---
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bump_version.ps1 -Mode patch
if errorlevel 1 (
    echo [ERROR] 版本号递增失败
    pause
    exit /b 1
)
echo.

rem --- 前置检查: node_modules ---
if not exist "frontend\node_modules" (
    echo [pre] 首次打包，装 npm 依赖...
    pushd frontend
    call npm install
    if errorlevel 1 (
        echo [ERROR] npm install 失败
        popd
        pause
        exit /b 1
    )
    popd
    echo.
)

rem --- 检查 sidecar exe 是否存在且最新（比对 backend/ 源码修改时间） ---
set "SIDECAR_EXE=frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe"
set "RUN_BACKEND=0"

if not exist "%SIDECAR_EXE%" (
    echo [pre] sidecar exe 不存在，需要首次编译后端...
    set "RUN_BACKEND=1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_backend_freshness.ps1 > "%TEMP%\lry_sidecar_status.tmp" 2>&1
    set "CHECK_CODE=!errorlevel!"
    if "!CHECK_CODE!"=="2" (
        echo.
        echo ============================================================
        echo   [WARN] 检测到后端 Python 源码已有更新（晚于现有 sidecar exe）！
        echo ============================================================
        for /f "usebackq tokens=1-4 delims=|" %%A in ("%TEMP%\lry_sidecar_status.tmp") do (
            if "%%A"=="STALE" (
                echo   当前 sidecar 生成时间: %%B
                echo   最新后端代码修改时间: %%C ^(%%D^)
            )
        )
        echo.
        echo   请选择操作:
        echo     [1] 重新编译后端并打包 ^(包含最新后端逻辑，约需 5~10 分钟^)
        echo     [2] 跳过后端编译 ^(推荐，使用现有 exe 快速打包，仅更新前端，约 2~3 分钟^)
        echo.
        set /p "USER_CHOICE=请输入选项 [默认 2，直接回车即跳过后端]: "
        if "!USER_CHOICE!"=="1" (
            echo [pre] 用户选择重新编译后端...
            set "RUN_BACKEND=1"
        ) else (
            echo [pre] 用户选择跳过后端编译，使用现有 sidecar...
            set "RUN_BACKEND=0"
        )
        echo.
    ) else (
        for /f "usebackq tokens=1,2 delims=|" %%A in ("%TEMP%\lry_sidecar_status.tmp") do (
            if "%%A"=="FRESH" (
                echo [pre] sidecar exe 为最新 ^(生成于 %%B^)，跳过后端编译（节省构建时间）
            )
        )
        echo [pre] sidecar exe 已就绪，跳过后端编译（节省构建时间）
        set "RUN_BACKEND=0"
    )
    if exist "%TEMP%\lry_sidecar_status.tmp" del "%TEMP%\lry_sidecar_status.tmp"
)
echo.

rem --- 设置 RC 版构建标志 ---
set "VITE_LITE_BUILD=1"
echo [pre] VITE_LITE_BUILD=1 (RC: companions + council only)
echo.

rem --- 避免旧运行进程占用导致文件锁与内存冲突 ---
taskkill /F /IM liuruyan-agent.exe >nul 2>&1
taskkill /F /IM liuruyan-backend.exe >nul 2>&1
taskkill /F /IM gcc.exe >nul 2>&1
taskkill /F /IM cc1.exe >nul 2>&1
taskkill /F /IM rustc.exe >nul 2>&1
set "NODE_OPTIONS=--max-old-space-size=4096"

rem --- 独立的 CARGO_TARGET_DIR，避免覆盖完整版的 MSI ---
for %%I in ("%~dp0..") do set "RC_TARGET_BASE=%%~fI\lry-target-rc"
set "CARGO_TARGET_DIR=%RC_TARGET_BASE%"
set "RUST_MIN_STACK=16777216"
echo [pre] CARGO_TARGET_DIR=%CARGO_TARGET_DIR% (isolated target dir)
echo [pre] RUST_MIN_STACK=%RUST_MIN_STACK%
if exist "!CARGO_TARGET_DIR!\release\bundle\nsis" rmdir /s /q "!CARGO_TARGET_DIR!\release\bundle\nsis" >nul 2>&1
if exist "!CARGO_TARGET_DIR!\release\bundle\msi" rmdir /s /q "!CARGO_TARGET_DIR!\release\bundle\msi" >nul 2>&1
if exist "!CARGO_TARGET_DIR!\release\nsis" rmdir /s /q "!CARGO_TARGET_DIR!\release\nsis" >nul 2>&1
if exist "!CARGO_TARGET_DIR!\release\wix" rmdir /s /q "!CARGO_TARGET_DIR!\release\wix" >nul 2>&1
if exist "!CARGO_TARGET_DIR!\release\bundle\nsis" (
    set "CARGO_TARGET_DIR=%RC_TARGET_BASE%-%RANDOM%"
    echo [pre] previous RC output dir is busy, fallback CARGO_TARGET_DIR=!CARGO_TARGET_DIR!
)
echo.

rem --- 正式打包 ---
echo [2/3] 构建 RC 版安装包...
echo.

if "!RUN_BACKEND!"=="1" (
    echo [build] Build backend + frontend + Tauri ^(about 5-10 minutes^)...
    set "PACKAGE_SCRIPT=package:lite:full"
) else (
    echo [build] Skip backend, build frontend + Tauri only ^(about 2-3 minutes^)...
    set "PACKAGE_SCRIPT=package:lite"
)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\package_dashboard.ps1 -ProjectRoot "%CD%" -PackageScript "!PACKAGE_SCRIPT!"
set BUILD_EXIT=%errorlevel%

if %BUILD_EXIT% neq 0 (
    echo.
    echo ============================================================
    echo   [FAIL] 打包失败，退出码: %BUILD_EXIT%
    echo   查看上面的日志定位问题
    echo ============================================================
    pause
    exit /b %BUILD_EXIT%
)

rem --- 成功汇报 ---
set "OUTPUT_DIR=!CARGO_TARGET_DIR!\release\bundle\nsis"
if not exist "!OUTPUT_DIR!" set "OUTPUT_DIR=!CARGO_TARGET_DIR!\release\bundle\msi"
if not exist "!OUTPUT_DIR!" set "OUTPUT_DIR=!CARGO_TARGET_DIR!\release\bundle"

echo.
echo ============================================================
echo   [OK] RC 版打包成功
echo ============================================================
echo 结束时间: %date% %time%
echo.

if exist "!OUTPUT_DIR!" (
    echo 安装包产物（RC 版，支持当前用户 / 所有用户双模式）:
    for /f "delims=" %%F in ('dir /b /o:-d "!OUTPUT_DIR!\*.exe" "!OUTPUT_DIR!\*.msi" 2^>nul') do (
        echo   !OUTPUT_DIR!\%%F
    )
    echo.
    echo [3/3] 打开 RC 版产物目录...
    explorer "!OUTPUT_DIR!"
) else (
    echo [WARN] 产物目录不存在: !OUTPUT_DIR!
)

echo.
echo （想改 minor/major 运行: scripts\bump_version.ps1 -Mode minor）

echo.
pause