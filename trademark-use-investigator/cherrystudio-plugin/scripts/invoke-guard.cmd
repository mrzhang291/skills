@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONDONTWRITEBYTECODE=1"

for %%I in ("%~dp0..") do set "PLUGIN_ROOT=%%~fI"
for %%I in ("%PLUGIN_ROOT%\..\..") do set "CLAUDE_ROOT=%%~fI"
set "GUARD=%CLAUDE_ROOT%\skills\trademark-use-investigator\scripts\cherrystudio-report-guard.py"

if not exist "%GUARD%" (
  1>&2 echo trademark-use-investigator guard is missing: %GUARD%
  exit /b 2
)

if defined TRADEMARK_USE_INVESTIGATOR_PYTHON if exist "%TRADEMARK_USE_INVESTIGATOR_PYTHON%" (
  set "GUARD_PYTHON=%TRADEMARK_USE_INVESTIGATOR_PYTHON%"
  goto run_python
)

where python.exe >nul 2>nul
if not errorlevel 1 (
  set "GUARD_PYTHON=python.exe"
  goto run_python
)

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~fD\python.exe" (
  set "GUARD_PYTHON=%%~fD\python.exe"
  goto run_python
)

for /d %%D in ("C:\Python3*") do if exist "%%~fD\python.exe" (
  set "GUARD_PYTHON=%%~fD\python.exe"
  goto run_python
)

for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%~fD\python.exe" (
  set "GUARD_PYTHON=%%~fD\python.exe"
  goto run_python
)

for /f "delims=" %%P in ('where py.exe 2^>nul') do (
  set "GUARD_PY_LAUNCHER=%%P"
  goto run_py_launcher
)

1>&2 echo trademark-use-investigator requires Python; run the official agent bootstrap first.
exit /b 2

:run_python
"%GUARD_PYTHON%" "%GUARD%"
exit /b %ERRORLEVEL%

:run_py_launcher
"%GUARD_PY_LAUNCHER%" -3 "%GUARD%"
exit /b %ERRORLEVEL%
