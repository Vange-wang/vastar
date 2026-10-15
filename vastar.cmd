@echo off
setlocal
set "VASTAR_HOME=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VASTAR_PY=python"
if exist "%VASTAR_HOME%.venv\Scripts\python.exe" set "VASTAR_PY=%VASTAR_HOME%.venv\Scripts\python.exe"
"%VASTAR_PY%" -X utf8 "%VASTAR_HOME%scripts\start_vastar.py" %*
exit /b %ERRORLEVEL%
