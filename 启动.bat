@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"

echo ========================================
echo   RuYan Agent Launcher
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

echo [1/3] Stopping old frontend and backend instances...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $frontendRoot=[IO.Path]::GetFullPath('%~dp0frontend\'); $launcher=[IO.Path]::GetFullPath('%~dp0backend\venv\Scripts\python.exe'); $all=Get-CimInstance Win32_Process; $frontend=$all | Where-Object { $_.ProcessId -ne $PID -and (($_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith($frontendRoot,[StringComparison]::OrdinalIgnoreCase)) -or ($_.CommandLine -and $_.CommandLine.IndexOf($frontendRoot,[StringComparison]::OrdinalIgnoreCase) -ge 0)) }; $frontendIds=@{}; foreach ($proc in $frontend) { $frontendIds[$proc.ProcessId]=$true }; $frontendRoots=$frontend | Where-Object { -not $frontendIds.ContainsKey($_.ParentProcessId) }; foreach ($proc in $frontendRoots) { taskkill.exe /PID $proc.ProcessId /T /F ^| Out-Null }; $owned=$all | Where-Object { $_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $launcher) -and $_.CommandLine -match 'main\.py' }; foreach ($proc in $owned) { $parent=$all | Where-Object ProcessId -eq $proc.ParentProcessId; if ($parent.Name -eq 'cmd.exe' -and $parent.CommandLine -match 'venv\\Scripts\\python\.exe +main\.py') { taskkill.exe /PID $parent.ProcessId /T /F ^| Out-Null } else { taskkill.exe /PID $proc.ProcessId /T /F ^| Out-Null } }; for ($i=0; $i -lt 32; $i++) { if (-not (Get-NetTCPConnection -LocalPort %LIURUYAN_DEV_PORT% -State Listen)) { exit 0 }; Start-Sleep -Milliseconds 250 }; exit 23"
if errorlevel 1 (
    echo [ERROR] Port %LIURUYAN_DEV_PORT% could not be released. Restart aborted.
    pause
    exit /b 1
)

echo [2/3] Starting backend (venv Python)...
cd /d "%~dp0backend"
start "RuYan-Backend-Dev-%LIURUYAN_DEV_PORT%" cmd /k "set LIURUYAN_DATA_DIR=%LIURUYAN_DATA_DIR% && set LIURUYAN_V2_CHANNELS=%LIURUYAN_V2_CHANNELS% && set LIURUYAN_MCP=%LIURUYAN_MCP% && venv\Scripts\python.exe main.py --host 127.0.0.1 --port %LIURUYAN_DEV_PORT%"

echo [3/3] Starting frontend (tauri dev)...
rem Frontend waitForBackend() polls /health until the new backend is ready.
cd /d "%~dp0frontend"
call npm run tauri dev

pause
