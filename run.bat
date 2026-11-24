@echo off
setlocal enabledelayedexpansion
title GURU Fundamental Scanner

echo ============================================================
echo   GURU Fundamental Scanner
echo ============================================================
echo.

:: ── Locate Python ────────────────────────────────────────────────────────────
set PYTHON_CMD=
for %%C in (python python3 py) do (
    if "!PYTHON_CMD!"=="" (
        where %%C >nul 2>&1
        if !errorlevel! == 0 (
            set PYTHON_CMD=%%C
        )
    )
)

if "%PYTHON_CMD%"=="" (
    echo ERROR: Python not found.
    echo Please install Python 3.9+ from https://python.org
    echo Make sure to tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Verify Python version >= 3.9
%PYTHON_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.9 or newer is required.
    echo Your version:
    %PYTHON_CMD% --version
    pause
    exit /b 1
)

echo Using: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.

:: ── Virtual environment ───────────────────────────────────────────────────────
if not exist venv (
    echo Creating virtual environment...
    %PYTHON_CMD% -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Activating virtual environment...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)

:: ── Install / update dependencies ────────────────────────────────────────────
echo Installing dependencies (this may take a few minutes on first run)...
pip install -q --upgrade pip
pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Dependency installation failed.
    echo Please check your internet connection and try again.
    pause
    exit /b 1
)
echo Dependencies OK.
echo.

:: ── First-time setup ─────────────────────────────────────────────────────────
if not exist guru_scanner.db (
    echo Running first-time setup...
    python setup.py
    if errorlevel 1 (
        echo WARNING: Setup encountered an issue. Continuing anyway...
    )
    echo.
)

:: ── Launch ────────────────────────────────────────────────────────────────────
echo Starting GURU Fundamental Scanner...
echo.
echo   Browser (this PC) : http://localhost:8501
echo   Browser (phone)   : http://YOUR-PC-IP:8501
echo   Find your IP      : Open Command Prompt and type  ipconfig
echo                       Look for "IPv4 Address" under your WiFi adapter
echo.
echo NOTE: If Windows Firewall asks to allow Python/Streamlit, click "Allow".
echo       This is needed so your phone can connect over WiFi.
echo.
echo Press Ctrl+C to stop.
echo.
streamlit run app.py --server.port=8501 --server.address=0.0.0.0

if errorlevel 1 (
    echo.
    echo App stopped with an error. See messages above.
    pause
)
