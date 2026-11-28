@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo ==================================================
echo   Auto Video Maker - Local Discovery Runner
echo ==================================================
echo.

set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [ERROR] Python was not found in .venv or PATH.
    pause
    exit /b 1
)

echo Choose output mode:
echo   [1] Draft locally only ^(no upload^)  [default]
echo   [2] Publish to YouTube
echo   [3] Publish to Instagram
echo   [4] Publish to Both ^(YouTube + Instagram^)
echo.
set "MODE_CHOICE="
set /p "MODE_CHOICE=Enter choice [1-4] (default 1): "
if defined MODE_CHOICE set "MODE_CHOICE=%MODE_CHOICE: =%"
if not defined MODE_CHOICE set "MODE_CHOICE=1"

set "PLATFORM=draft"
if "%MODE_CHOICE%"=="1" set "PLATFORM=draft"
if "%MODE_CHOICE%"=="2" set "PLATFORM=youtube"
if "%MODE_CHOICE%"=="3" set "PLATFORM=instagram"
if "%MODE_CHOICE%"=="4" set "PLATFORM=both"

if "%PLATFORM%"=="draft" if not "%MODE_CHOICE%"=="1" (
    echo [ERROR] Invalid choice "%MODE_CHOICE%". Choose 1, 2, 3, or 4.
    pause
    exit /b 1
)

echo.
set "COUNT="
set /p "COUNT=How many Shorts to generate? [1-5] (default 1): "
if defined COUNT set "COUNT=%COUNT: =%"
if not defined COUNT set "COUNT=1"

set "EXTRA_ARGS="
set "SECONDS="
if "%COUNT%"=="1" (
    set /p "SECONDS=Optional target seconds [12-85] ^(press Enter for audio-driven^): "
    if defined SECONDS set "SECONDS=!SECONDS: =!"
    if defined SECONDS set "EXTRA_ARGS=!EXTRA_ARGS! --seconds !SECONDS!"
)

set "DELAY_SECONDS=0"
if not "%COUNT%"=="1" set "DELAY_SECONDS=30"

set "USE_LOOP="
set /p "USE_LOOP=Seamless loop without outro card? [y/N]: "
if defined USE_LOOP set "USE_LOOP=!USE_LOOP: =!"
if /i "%USE_LOOP%"=="y" set "EXTRA_ARGS=!EXTRA_ARGS! --loop"

set "USE_HQ="
set /p "USE_HQ=Use Gemini 4 Hydrogen for script? [y/N]: "
if defined USE_HQ set "USE_HQ=!USE_HQ: =!"
if /i "%USE_HQ%"=="y" set "EXTRA_ARGS=!EXTRA_ARGS! --hq-script"

echo.
echo Running: %PY% shorts.py discover-batch --count %COUNT% --platform %PLATFORM% --delay-seconds %DELAY_SECONDS% --qa-gate!EXTRA_ARGS!
echo.
%PY% shorts.py discover-batch --count %COUNT% --platform %PLATFORM% --delay-seconds %DELAY_SECONDS% --qa-gate!EXTRA_ARGS!
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [DONE] Finished! Check output\ for rendered files.
) else (
    echo [FAILED] Exited with code %EXIT_CODE%.
)
echo.
pause
exit /b %EXIT_CODE%
