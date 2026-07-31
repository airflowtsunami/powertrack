@echo off
setlocal
cd /d "%~dp0"

set "VENV_PYTHON=.venv\Scripts\python.exe"

rem Create the environment if this is the first run.
if not exist "%VENV_PYTHON%" (
    echo PowerTrack has not been installed yet.
    echo Running the installer...
    echo.
    call install.bat
)

rem Stop if installation did not create a working Python environment.
if not exist "%VENV_PYTHON%" (
    echo.
    echo Installation did not complete successfully.
    pause
    exit /b 1
)

rem Repair the environment if any required Python packages are missing.
"%VENV_PYTHON%" -c "import pygame, openant, usb" >nul 2>&1
if errorlevel 1 (
    echo One or more required packages are missing.
    echo Running the installer to repair the environment...
    echo.
    call install.bat
)

rem Verify the dependencies before launching the game.
"%VENV_PYTHON%" -c "import pygame, openant, usb" >nul 2>&1
if errorlevel 1 (
    echo.
    echo PowerTrack could not install or load all required packages.
    pause
    exit /b 1
)

"%VENV_PYTHON%" powertrack.py

if errorlevel 1 (
    echo.
    echo PowerTrack closed with an error.
    pause
)

endlocal
