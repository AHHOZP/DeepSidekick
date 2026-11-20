@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem DeepSidekick WebUI launcher. Keep this file compatible with cmd.exe.
rem Keep CRLF line endings; cmd.exe misparses LF batch files.
set "PYTHONIOENCODING=utf-8"
set "PY=.venv\Scripts\python.exe"

if exist "%PY%" goto have_venv
echo [X] 没有 .venv。先跑：
echo     python -m venv .venv
echo     .venv\Scripts\python -m pip install --no-cache-dir -r requirements.txt
pause
exit /b 1

:have_venv
rem Resolve the real base interpreter and the venv site-packages.
set "BASE="
set "SITE="
set "TMP1=%TEMP%\deepsidekick-base-%RANDOM%%RANDOM%.txt"
set "TMP2=%TEMP%\deepsidekick-site-%RANDOM%%RANDOM%.txt"

"%PY%" -c "import sys; print(getattr(sys,'_base_executable','') or sys.executable)" > "%TMP1%"
if not exist "%TMP1%" goto after_base
set /p "BASE="<"%TMP1%"
del "%TMP1%" >nul 2>nul
:after_base
"%PY%" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" > "%TMP2%"
if not exist "%TMP2%" goto after_site
set /p "SITE="<"%TMP2%"
del "%TMP2%" >nul 2>nul
:after_site

if defined BASE goto base_ready
set "BASE=%PY%"
:base_ready
if exist "%BASE%" goto base_exists
set "BASE=%PY%"
:base_exists
if not exist "%SITE%" goto launch
if not defined PYTHONPATH goto site_only
set "PYTHONPATH=%SITE%;%PYTHONPATH%"
goto site_done
:site_only
set "PYTHONPATH=%SITE%"
:site_done
set "VIRTUAL_ENV=%CD%\.venv"

:launch
"%BASE%" -m sidekick.webui
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="0" goto success
echo.
echo [错误] 程序异常退出，错误码: %CODE%
pause
exit /b %CODE%

:success
exit /b 0
