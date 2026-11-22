@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Chopify Studio necesita el entorno .venv con Python 3.11 o posterior.
  echo Abre docs\WINDOWS-STUDIO.md para preparar la instalacion inicial.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0studio_launcher.py"
