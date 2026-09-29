@echo off
setlocal

set "SKILL_DIR=%~dp0"
set "PROJECT_ROOT=%SKILL_DIR%..\.."
set "PORT=18788"
set "TOKEN_PORT=18787"
set "CODEGEEX_DEBUG_LAST_PATH=%SKILL_DIR%last_debug.json"

echo [CodeGeeX Bridge] skill dir: %SKILL_DIR%
echo [CodeGeeX Bridge] project root: %PROJECT_ROOT%
echo [CodeGeeX Bridge] checking token service on 127.0.0.1:%TOKEN_PORT% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:%TOKEN_PORT%/token' -Method Get -TimeoutSec 5; if ($r.token -or $r.'code-token' -or $r.code_token) { exit 0 } else { exit 2 } } catch { exit 1 }"
if errorlevel 1 (
  echo.
  echo [ERROR] CodeGeeX token service is not available on 127.0.0.1:%TOKEN_PORT%.
  echo Please open VS Code with CodeGeeX logged in first, then run this file again.
  echo.
  pause
  exit /b 1
)

echo [CodeGeeX Bridge] stopping old bridge on port %PORT% if exists ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$conns = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; foreach ($c in $conns) { try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop; Write-Host ('stopped pid ' + $c.OwningProcess) } catch {} }"

echo [CodeGeeX Bridge] starting http://127.0.0.1:%PORT% ...
cd /d "%PROJECT_ROOT%"
python "%SKILL_DIR%scripts\codegeex_bridge.py" --host 127.0.0.1 --port %PORT%

pause
