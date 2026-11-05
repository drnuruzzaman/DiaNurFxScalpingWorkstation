@echo off
rem DiaNurFx - one-click start. Opens the launcher window (no console).
cd /d "%~dp0"

rem Python 3.12, found by folder - never "whatever pythonw comes first". This PC
rem has 3.12, 3.13, 3.14 and 3.15 side by side, and PATH order changes as they
rem come and go: on 2026-11-04 it handed the launcher 3.15, which MetaTrader5
rem has no build for, and nothing could start. 3.12 is what the bridge's
rem MetaTrader5 and the API's packages are installed into.
set "PYDIR="
for %%D in ("%LOCALAPPDATA%\Programs\Python\Python312" "%ProgramFiles%\Python312" "C:\Python312") do (
  if not defined PYDIR if exist "%%~D\pythonw.exe" set "PYDIR=%%~D"
)
rem Elsewhere? Ask the py launcher where its 3.12 lives.
if not defined PYDIR (
  for /f "delims=" %%E in ('py -3.12 -c "import sys, os; print(os.path.dirname(sys.executable))" 2^>nul') do set "PYDIR=%%E"
)
if not defined PYDIR (
  echo Could not find Python 3.12. Install it from python.org, then run %~nx0 again.
  pause
  exit /b 1
)

rem This PC's machine PATH is malformed (no System32, a literal %%path%%, entries
rem ending in a stray quote), so the netstat, tasklist, cmd and npm the launcher
rem runs can go unfound. Put the folders it needs first, for this launch only;
rem the launcher and everything it starts inherit it.
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PYDIR%;%PYDIR%\Scripts;%ProgramFiles%\nodejs;%APPDATA%\npm;%ProgramFiles%\Git\cmd;%PATH%"

start "" "%PYDIR%\pythonw.exe" tools\launcher.py
