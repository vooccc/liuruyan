@echo off
setlocal
cd /d "%~dp0"
title Liuruyan Release to GitHub

if exist "backend\venv\Scripts\python.exe" (
    "backend\venv\Scripts\python.exe" "scripts\release_github.py"
) else (
    python "scripts\release_github.py"
)

pause
