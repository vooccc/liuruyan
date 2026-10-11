@echo off
REM Liuruyan VSCode Extension - One-click Publish
REM Workflow: bump patch version -> compile -> package -> install
REM Usage: double-click, or .\publish.bat from this directory

cd /d "%~dp0"

echo.
echo [1/5] Bumping patch version...
call npm version patch --no-git-tag-version
if errorlevel 1 goto :error

for /f "delims=" %%v in ('node -p "require('./package.json').version"') do set VERSION=%%v
set VSIX=liuruyan-vscode-%VERSION%.vsix
echo       new version = %VERSION%

echo.
echo [2/5] Cleaning old vsix files...
for %%f in (liuruyan-vscode-*.vsix) do (
    if not "%%f"=="%VSIX%" (
        echo       deleting %%f
        del /q "%%f"
    )
)

echo.
echo [3/5] Compiling TypeScript...
call npm run compile
if errorlevel 1 goto :error

echo.
echo [4/5] Packaging vsix...
call npx vsce package --allow-missing-repository --skip-license
if errorlevel 1 goto :error

echo.
echo [5/5] Installing %VSIX% to VSCode...
call code --install-extension "%VSIX%" --force
if errorlevel 1 goto :error

echo.
echo ============================================================
echo  SUCCESS: %VSIX% installed
echo  Next: Ctrl+Shift+P  Developer: Reload Window
echo ============================================================
echo.

if "%~1"=="" pause
exit /b 0


:error
echo.
echo ============================================================
echo  FAILED. See above. Press any key to close.
echo ============================================================
pause
exit /b 1
