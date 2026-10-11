@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"

echo ========================================
echo   柳如烟 (Liuruyan) · 开发模式启动器
echo ========================================
echo.

rem Dev mode: use ~/.liuruyan-dev (isolated from prod ~/.liuruyan and git source tree).
set "LIURUYAN_DATA_DIR=%USERPROFILE%\.liuruyan-dev"
if not exist "%LIURUYAN_DATA_DIR%" mkdir "%LIURUYAN_DATA_DIR%"

rem v2 channels (Phase E) opt-in. set 0 to disable.
set "LIURUYAN_V2_CHANNELS=1"

rem MCP subsystem opt-in (M1+M2+M3+M4 done, 61 tests green). set 0 to disable.
set "LIURUYAN_MCP=1"

rem Dev and installed builds run side by side: dev uses 8002, prod keeps 8001.
set "LIURUYAN_DEV_PORT=8002"
set "VITE_API_BASE=http://127.0.0.1:%LIURUYAN_DEV_PORT%"

rem --- [0/3] 前置环境检查与秒级初始化 ---
if not exist "backend\venv\Scripts\python.exe" (
    if exist "..\柳如烟\backend\venv\Scripts\python.exe" (
        echo [info] 检测到同级工程主虚拟环境，正在建立秒级软联接...
        if not exist "backend" mkdir backend
        mklink /J "backend\venv" "..\柳如烟\backend\venv" >nul 2>&1
    )
)
if not exist "backend\venv\Scripts\python.exe" (
    echo [info] 首次运行：未检测到后端虚拟环境，正在自动初始化 ^(backend\venv^)...
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] 未检测到系统 Python，请先安装 Python 3.10+ 并加入系统环境变量 PATH。
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
    echo [info] 正在安装后端依赖 ^(pip install -r requirements.txt^)...
    venv\Scripts\pip install -r requirements.txt
    if errorlevel 1 (
        echo [WARN] 依赖安装遇到警告或网络问题，若后续启动报错请手动执行 pip install -r requirements.txt
    )
    popd
    echo.
)

if not exist "frontend\node_modules" (
    if exist "..\柳如烟\frontend\node_modules" (
        echo [info] 检测到同级工程前端依赖，正在建立秒级软联接...
        mklink /J "frontend\node_modules" "..\柳如烟\frontend\node_modules" >nul 2>&1
    )
)
if not exist "frontend\node_modules" (
    echo [info] 首次运行：未检测到前端依赖，正在自动安装 ^(npm install^)...
    where npm >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] 未检测到 Node.js / npm，请先安装 Node.js 18+。
        echo 下载地址: https://nodejs.org/
        pause
        exit /b 1
    )
    pushd frontend
    call npm install
    if errorlevel 1 (
        echo [ERROR] 前端 npm install 失败，请检查网络或重试。
        popd
        pause
        exit /b 1
    )
    popd
    echo.
)

echo [1/3] 正在检查并释放端口与历史进程...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $frontendRoot=[IO.Path]::GetFullPath('%~dp0frontend\'); $launcher=[IO.Path]::GetFullPath('%~dp0backend\venv\Scripts\python.exe'); $all=Get-CimInstance Win32_Process; foreach ($p in $all) { if ($p.ProcessId -ne $PID -and (($p.ExecutablePath -and [IO.Path]::GetFullPath($p.ExecutablePath).StartsWith($frontendRoot,[StringComparison]::OrdinalIgnoreCase)) -or ($p.CommandLine -and $p.CommandLine.IndexOf($frontendRoot,[StringComparison]::OrdinalIgnoreCase) -ge 0) -or ($p.ExecutablePath -and [IO.Path]::GetFullPath($p.ExecutablePath) -ieq $launcher -and $p.CommandLine -match 'main\.py'))) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; if ($p.ParentProcessId) { Stop-Process -Id $p.ParentProcessId -Force -ErrorAction SilentlyContinue } } }; $conns=Get-NetTCPConnection -LocalPort %LIURUYAN_DEV_PORT% -State Listen -ErrorAction SilentlyContinue; foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }; for ($i=0; $i -lt 32; $i++) { if (-not (Get-NetTCPConnection -LocalPort %LIURUYAN_DEV_PORT% -State Listen -ErrorAction SilentlyContinue)) { exit 0 }; Start-Sleep -Milliseconds 250 }; exit 23"
if errorlevel 1 (
    echo [ERROR] 端口 %LIURUYAN_DEV_PORT% 无法释放，请关闭占用进程后重试。
    pause
    exit /b 1
)

echo [2/3] 正在启动后端服务 (FastAPI / Port %LIURUYAN_DEV_PORT%)...
cd /d "%~dp0backend"
start "柳如烟-后端服务-Dev-%LIURUYAN_DEV_PORT%" cmd /k "set "LIURUYAN_DATA_DIR=%LIURUYAN_DATA_DIR%" && set "LIURUYAN_V2_CHANNELS=%LIURUYAN_V2_CHANNELS%" && set "LIURUYAN_MCP=%LIURUYAN_MCP%" && venv\Scripts\python.exe main.py --host 127.0.0.1 --port %LIURUYAN_DEV_PORT%"

echo [3/3] 正在启动桌面端界面 (Tauri Dev)...
rem Frontend waitForBackend() polls /health until the new backend is ready.
cd /d "%~dp0frontend"
call npm run tauri dev

pause
