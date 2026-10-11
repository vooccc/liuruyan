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

rem --- [0/3] 开源文件夹打包：先从私有主工程同步最新代码（私有主工程自身打包则跳过） ---
rem 注意：本步骤必须先于版本号递增执行——export 会用私有工程的 tauri.conf.json 覆盖本文件夹版本号
if exist "scripts\export_opensource.py" (
    echo [info] 当前为私有主工程，跳过开源代码同步
    goto :AFTER_OPENSOURCE_SYNC
)

if exist "..\柳如烟\scripts\export_opensource.py" (
    echo --- [0/3] 检测到同级私有主工程，正在同步最新代码到本开源文件夹 ---
    python "..\柳如烟\scripts\export_opensource.py" --target "%~dp0." --message "chore: sync with core updates for release packaging"
    if errorlevel 1 (
        echo [ERROR] 开源代码同步失败，请手动执行后重试:
        echo   python "..\柳如烟\scripts\export_opensource.py" --target "%~dp0."
        pause
        exit /b 1
    )
    echo.
)

:AFTER_OPENSOURCE_SYNC

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
if exist "backend\venv\Scripts\python.exe" goto :VENV_READY
if not exist "..\柳如烟\backend\venv\Scripts\python.exe" goto :VENV_INIT_SELF
echo [info] 检测到同级工程主虚拟环境，正在建立秒级软联接...
powershell -NoProfile -Command "New-Item -ItemType Junction -Path 'backend\venv' -Target (Resolve-Path '..\柳如烟\backend\venv') -Force" >nul 2>&1
if exist "backend\venv\Scripts\python.exe" goto :VENV_READY

:VENV_INIT_SELF
echo [info] 未检测到后端虚拟环境，正在自动初始化 (backend\venv)...
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 Python，请先安装 Python 3.10+ 并加入系统环境变量 PATH。
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
pushd backend
python -m venv venv
if errorlevel 1 (
    echo [ERROR] 后端虚拟环境创建失败。
    popd
    pause
    exit /b 1
)
echo [info] 正在安装后端依赖 (pip install -r requirements.txt)...
venv\Scripts\pip install -r requirements.txt
popd

:VENV_READY

rem --- 前置检查 2: ripgrep 二进制 ---
if exist "vendor\ripgrep\x64-win32\rg.exe" goto :RIPGREP_READY
if not exist "..\柳如烟\vendor\ripgrep\x64-win32\rg.exe" goto :RIPGREP_DOWNLOAD
powershell -NoProfile -Command "New-Item -ItemType Junction -Path 'vendor\ripgrep' -Target (Resolve-Path '..\柳如烟\vendor\ripgrep') -Force" >nul 2>&1
if exist "vendor\ripgrep\x64-win32\rg.exe" goto :RIPGREP_READY

:RIPGREP_DOWNLOAD
echo [pre] 未发现 vendor\ripgrep\x64-win32\rg.exe，调 bootstrap 下载...
"backend\venv\Scripts\python.exe" "scripts\bootstrap_ripgrep.py"
if errorlevel 1 (
    echo [WARN] ripgrep bootstrap 失败，sidecar 将退到 Python fallback
    echo        可联网后手动跑: python scripts\bootstrap_ripgrep.py
    echo.
)

:RIPGREP_READY

rem --- 前置检查 3: node_modules ---
if exist "frontend\node_modules" goto :NODE_MODULES_READY
if not exist "..\柳如烟\frontend\node_modules" goto :NPM_INSTALL
echo [info] 检测到同级工程前端依赖，正在建立秒级软联接...
powershell -NoProfile -Command "New-Item -ItemType Junction -Path 'frontend\node_modules' -Target (Resolve-Path '..\柳如烟\frontend\node_modules') -Force" >nul 2>&1
if exist "frontend\node_modules" goto :NODE_MODULES_READY

:NPM_INSTALL
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

:NODE_MODULES_READY

rem --- 前置检查 4: backend sidecar 二进制 ---
set "PKG_SCRIPT=package"
if exist "frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe" goto :SIDECAR_READY
if exist "backend\dist\liuruyan-backend.exe" (
    if not exist "frontend\src-tauri\binaries" mkdir "frontend\src-tauri\binaries"
    copy /y "backend\dist\liuruyan-backend.exe" "frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe" >nul 2>&1
    goto :SIDECAR_READY
)
if exist "..\柳如烟\frontend\src-tauri\binaries" (
    echo [info] 检测到同级工程后端二进制，正在建立秒级软联接...
    powershell -NoProfile -Command "New-Item -ItemType Junction -Path 'frontend\src-tauri\binaries' -Target (Resolve-Path '..\柳如烟\frontend\src-tauri\binaries') -Force" >nul 2>&1
)

:SIDECAR_READY

if not exist "frontend\src-tauri\binaries\liuruyan-backend-x86_64-pc-windows-msvc.exe" goto :NO_SIDECAR

echo ============================================================
echo [info] 检测到已就绪的后端 sidecar (liuruyan-backend.exe)
echo       跳过重新编译后端可直接为你节省约 6 分钟时间！
echo ============================================================
set /p REBUILD_BACKEND="是否跳过后端重编，极速仅打包桌面端安装包? (Y/n, 回车默认 Y): "
if /i "%REBUILD_BACKEND%"=="n" goto :NO_SIDECAR
set "PKG_SCRIPT=package:lite"
echo [mode] 极速模式: 跳过后端编译，直接进行 Tauri 桌面端封装...
echo.
goto :AFTER_SIDECAR_CHECK

:NO_SIDECAR
set "PKG_SCRIPT=package"
echo [mode] 全量模式: 编译后端 sidecar 并封装 Tauri 安装包...
echo.

:AFTER_SIDECAR_CHECK

rem --- 修复: Tauri build script 在中文路径下读 permissions toml 编码崩 ---
for %%I in ("%~dp0..") do set "CARGO_TARGET_DIR=%%~fI\lry-target"
set "RUST_MIN_STACK=8388608"
echo [pre] CARGO_TARGET_DIR=%CARGO_TARGET_DIR%
echo [pre] RUST_MIN_STACK=%RUST_MIN_STACK%
echo.

rem --- 避免旧运行进程占用导致文件锁与内存冲突 ---
taskkill /F /IM liuruyan-agent.exe >nul 2>&1
taskkill /F /IM liuruyan-backend.exe >nul 2>&1
taskkill /F /IM gcc.exe >nul 2>&1
taskkill /F /IM cc1.exe >nul 2>&1
taskkill /F /IM rustc.exe >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'build_backend\.ps1|tauri build|package_build') -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
set "NODE_OPTIONS=--max-old-space-size=4096"

rem --- 正式打包 ---
echo [2/3] 构建桌面端安装包...
echo [info] 模式: %PKG_SCRIPT% (Tauri + Vite + NSIS 安装包封装)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\package_dashboard.ps1 -ProjectRoot "%CD%" -PackageScript "%PKG_SCRIPT%"
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
if not exist "%OUTPUT_DIR%" set "OUTPUT_DIR=%CARGO_TARGET_DIR%\release\bundle"

echo.
echo ============================================================
echo   [OK] 打包成功
echo ============================================================
echo 结束时间: %date% %time%
echo.

if exist "%OUTPUT_DIR%" (
    for /f "delims=" %%F in ('dir /b /a-d "%OUTPUT_DIR%\柳如烟*.exe" 2^>nul') do (
        set "ORIG_NAME=%%F"
        set "ENG_NAME=!ORIG_NAME:柳如烟=liuruyan!"
        if not exist "%OUTPUT_DIR%\!ENG_NAME!" (
            copy /y "%OUTPUT_DIR%\%%F" "%OUTPUT_DIR%\!ENG_NAME!" >nul 2>&1
        )
    )
    echo 安装包产物（.exe 独立安装包，支持当前用户/所有用户双模式）:
    for /f "delims=" %%F in ('dir /b /o:-d "%OUTPUT_DIR%\*.exe" 2^>nul') do (
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

if exist "一键发布.bat" (
    set /p AUTO_PUB="是否立即将最新安装包自动发布至 GitHub Releases? (y/n, 默认 n): "
    if /i "!AUTO_PUB!"=="y" (
        echo.
        call "一键发布.bat"
    )
)

echo.
pause