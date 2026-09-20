@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion

:: ============================================================
::   Liuruyan Full Edition Package Builder
:: ============================================================

title 柳如烟 一键打包
cd /d "%~dp0"

echo ============================================================
echo   柳如烟 一键打包（完整版）
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
for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bump_version.ps1 -Mode print 2^>nul`) do (
    echo [version] %%V
)
echo.

rem --- 前置检查 1: backend venv ---
if not exist "backend\venv\Scripts\python.exe" (
    echo [ERROR] 未找到 backend\venv\Scripts\python.exe
    echo.
    echo 请先在 backend/ 下建虚拟环境并安装依赖:
    echo   cd backend
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem --- 前置检查 2: ripgrep 二进制 ---
if not exist "vendor\ripgrep\x64-win32\rg.exe" (
    echo [pre] 未发现 vendor\ripgrep\x64-win32\rg.exe，调 bootstrap 下载...
    "backend\venv\Scripts\python.exe" "scripts\bootstrap_ripgrep.py"
    if errorlevel 1 (
        echo [WARN] ripgrep bootstrap 失败，sidecar 将退到 Python fallback
        echo        可联网后手动跑: python scripts\bootstrap_ripgrep.py
        echo.
    )
)

rem --- 前置检查 3: node_modules ---
if not exist "frontend\node_modules" (
    echo [1/3] 首次打包，装 npm 依赖...
    pushd frontend
    call npm install
    if errorlevel 1 (
        echo.
        echo [ERROR] npm install 失败
        popd
        pause
        exit /b 1
    )
    popd
    echo.
)

rem --- 修复: Tauri build script 在中文路径下读 permissions toml 编码崩 ---
for %%I in ("%~dp0..") do set "CARGO_TARGET_DIR=%%~fI\lry-target"
set "RUST_MIN_STACK=16777216"
echo [pre] CARGO_TARGET_DIR=%CARGO_TARGET_DIR%
echo [pre] RUST_MIN_STACK=%RUST_MIN_STACK%
echo.

rem --- 避免旧运行进程占用导致文件锁与内存冲突 ---
taskkill /F /IM liuruyan-agent.exe >nul 2>&1
taskkill /F /IM liuruyan-backend.exe >nul 2>&1
taskkill /F /IM gcc.exe >nul 2>&1
taskkill /F /IM cc1.exe >nul 2>&1
taskkill /F /IM rustc.exe >nul 2>&1
set "NODE_OPTIONS=--max-old-space-size=4096"

rem --- 正式打包 ---
echo [2/3] 构建后端 sidecar exe 并封装安装包...
echo [info] PyInstaller + tsc + vite + tauri (纯净极速构建)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\package_dashboard.ps1 -ProjectRoot "%CD%"
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
set "OUTPUT_DIR=%CARGO_TARGET_DIR%\release\bundle\nsis"
if not exist "%OUTPUT_DIR%" set "OUTPUT_DIR=%CARGO_TARGET_DIR%\release\bundle\msi"
if not exist "%OUTPUT_DIR%" set "OUTPUT_DIR=%CARGO_TARGET_DIR%\release\bundle"

echo.
echo ============================================================
echo   [OK] 打包成功
echo ============================================================
echo 结束时间: %date% %time%
echo.

if exist "%OUTPUT_DIR%" (
    echo 安装包产物（支持当前用户 / 所有用户双模式）:
    for /f "delims=" %%F in ('dir /b /o:-d "%OUTPUT_DIR%\*.exe" "%OUTPUT_DIR%\*.msi" 2^>nul') do (
        echo   %OUTPUT_DIR%\%%F
    )
    echo.
    echo [3/3] 打开产物目录...
    explorer "%OUTPUT_DIR%"
) else (
    echo [WARN] 产物目录不存在: %OUTPUT_DIR%
)

echo.
echo （想改 minor/major 运行: scripts\bump_version.ps1 -Mode minor）

echo.
pause