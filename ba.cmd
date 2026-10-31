@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if "%~1"=="" (
  where pythonw >nul 2>nul
  if errorlevel 1 (
    python -m bili_audio gui
  ) else (
    start "" pythonw -m bili_audio gui
  )
  exit /b 0
)

python -m bili_audio %*
if errorlevel 1 pause
