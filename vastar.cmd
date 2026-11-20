@echo off
rem ============================================================
rem  Vastar launcher (canonical, versioned in repo)
rem  Usage:  vastar [args]        (cmd / PowerShell / Windows Terminal)
rem  This file is AUTHORITATIVE. The PATH shim in
rem  %USERPROFILE%\.local\bin\vastar.cmd only calls this one.
rem  ASCII only - do not put non-ASCII characters in this file.
rem  Preserve caller cwd; --project selects identity, --show-paths diagnoses paths.
rem ============================================================
setlocal
set "VASTAR_HOME=%~dp0"
if "%VASTAR_HOME:~-1%"=="\" set "VASTAR_HOME=%VASTAR_HOME:~0,-1%"

rem UTF-8 on the Python side (console codepage is left untouched)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if exist "%VASTAR_HOME%\runtime\python\python.exe" (
  set "VASTAR_PY=%VASTAR_HOME%\runtime\python\python.exe"
) else if exist "%VASTAR_HOME%\.venv\Scripts\python.exe" (
  set "VASTAR_PY=%VASTAR_HOME%\.venv\Scripts\python.exe"
) else (
  set "VASTAR_PY=python"
)

if not exist "%VASTAR_HOME%\main.py" (
  echo [vastar] main.py not found - the phase-1 runtime is not implemented yet.
  echo [vastar] repo    : %VASTAR_HOME%
  echo [vastar] expected: main.py ^(see collab/tasks/2026-09-20-phase1-implementation-task_zc.md^)
  endlocal & exit /b 3
)

"%VASTAR_PY%" -X utf8 "%VASTAR_HOME%\scripts\start_vastar.py" %*
set "VASTAR_RC=%ERRORLEVEL%"
endlocal & exit /b %VASTAR_RC%
