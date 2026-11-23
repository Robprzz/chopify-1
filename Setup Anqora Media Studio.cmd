@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Anqora Media Studio necesita el entorno .venv con Python 3.11 o posterior.
  echo Abre docs\WINDOWS-STUDIO.md para preparar la instalacion inicial.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 "%~dp0studio_launcher.py"
