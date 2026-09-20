@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion

title 柳如烟 - 一键发布安装包到 GitHub Releases
cd /d "%~dp0"

echo ============================================================
echo   柳如烟 (Ruyan AI) · 一键发布到 GitHub Releases
echo ============================================================
echo.

if exist "backend\venv\Scripts\python.exe" (
    "backend\venv\Scripts\python.exe" "scripts\release_github.py"
) else (
    python "scripts\release_github.py"
)

echo.
pause
