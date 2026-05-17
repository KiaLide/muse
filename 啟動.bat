@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Muse

echo.
echo  ================================================
echo   Muse - Music Preference Analysis
echo  ================================================
echo.

:: =========================================================
::  STEP 1: Find Python 3.10+
:: =========================================================
set "PYTHON="

:: Try py launcher first (picks the newest installed Python)
where py >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=2" %%V in ('py -3 --version 2^>^&1') do (
        for /f "tokens=1,2 delims=." %%M in ("%%V") do (
            if "%%M"=="3" if %%N GEQ 10 set "PYTHON=py -3"
        )
    )
)

:: Try python / python3 commands
if "!PYTHON!"=="" (
    for %%c in (python3 python) do (
        if "!PYTHON!"=="" (
            %%c --version >nul 2>&1
            if not errorlevel 1 (
                for /f "tokens=2" %%V in ('%%c --version 2^>^&1') do (
                    for /f "tokens=1,2 delims=." %%M in ("%%V") do (
                        if "%%M"=="3" if %%N GEQ 10 set "PYTHON=%%c"
                    )
                )
            )
        )
    )
)

:: Search known install locations
if "!PYTHON!"=="" (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
        "%LOCALAPPDATA%\Python\pythoncore-3.13-64\python.exe"
        "%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
    ) do (
        if "!PYTHON!"=="" if exist %%P set "PYTHON=%%~P"
    )
)

if not "!PYTHON!"=="" (
    echo  [OK] Python found: !PYTHON!
    goto :CHECK_FFMPEG
)

:: ---------------------------------------------------------
::  Python not found - install automatically
:: ---------------------------------------------------------
echo  [!!] Python 3.10+ not found. Installing...
echo.

:: Try winget (built into Windows 10 1709+ and Windows 11)
where winget >nul 2>&1
if not errorlevel 1 (
    echo       Trying winget...
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
    if not errorlevel 1 (
        echo       Refreshing PATH...
        for /f "usebackq tokens=*" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('PATH','User')"`) do set "PATH=%%P;%PATH%"
        if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        where py >nul 2>&1
        if not errorlevel 1 if "!PYTHON!"=="" py -3.12 --version >nul 2>&1 && set "PYTHON=py -3.12"
        if not "!PYTHON!"=="" goto :PY_DONE
    )
    echo       winget failed, trying direct download...
    echo.
)

:: Download installer from python.org
echo       Downloading Python 3.12 installer (~25 MB)...
set "PY_EXE=%TEMP%\python_setup.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12,Tls13'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe' -OutFile '!PY_EXE!' -UseBasicParsing"

if not exist "!PY_EXE!" (
    echo.
    echo  [ERROR] Could not download Python.
    echo.
    echo  Please install manually:
    echo    1. Go to https://python.org/downloads
    echo    2. Download Python 3.12 or later
    echo    3. During install, check "Add Python to PATH"
    echo    4. Re-run this file
    echo.
    pause
    exit /b 1
)

echo       Running installer (silent)...
"!PY_EXE!" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_doc=0
del "!PY_EXE!" >nul 2>&1

for /f "usebackq tokens=*" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('PATH','User')"`) do set "PATH=%%P;%PATH%"
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
where py >nul 2>&1
if not errorlevel 1 if "!PYTHON!"=="" py -3.12 --version >nul 2>&1 && set "PYTHON=py -3.12"

:PY_DONE
if "!PYTHON!"=="" (
    echo.
    echo  [ERROR] Python installed but still not detected.
    echo          Please reboot and try again.
    echo.
    pause
    exit /b 1
)
echo  [OK] Python installed: !PYTHON!

:: =========================================================
::  STEP 2: Find FFmpeg
:: =========================================================
:CHECK_FFMPEG

:: Add local ffmpeg folder to PATH if it exists (from a previous install)
if exist "%~dp0ffmpeg\ffmpeg.exe" set "PATH=%~dp0ffmpeg;%PATH%"

where ffmpeg >nul 2>&1
if not errorlevel 1 (
    echo  [OK] FFmpeg found
    goto :INSTALL_DEPS
)

:: ---------------------------------------------------------
::  FFmpeg not found - install automatically
:: ---------------------------------------------------------
echo  [!!] FFmpeg not found. Installing...
echo.

:: Try winget
where winget >nul 2>&1
if not errorlevel 1 (
    echo       Trying winget...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent
    if not errorlevel 1 (
        for /f "usebackq tokens=*" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('PATH','Machine')"`) do set "PATH=%%P;%PATH%"
        where ffmpeg >nul 2>&1
        if not errorlevel 1 (
            echo  [OK] FFmpeg installed via winget
            goto :INSTALL_DEPS
        )
        :: winget installed it but PATH not updated yet - search winget packages
        for /f "usebackq tokens=*" %%F in (`powershell -NoProfile -Command "Get-ChildItem '$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*' -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty DirectoryName"`) do (
            if exist "%%F\ffmpeg.exe" (
                set "PATH=%%F;%PATH%"
                echo  [OK] FFmpeg found at: %%F
                goto :INSTALL_DEPS
            )
        )
    )
    echo       winget failed, trying direct download...
    echo.
)

:: Download static build to local ffmpeg\ folder
echo       Downloading FFmpeg static build (~80 MB)...
set "FF_ZIP=%TEMP%\ffmpeg_dl.zip"
set "FF_TMP=%TEMP%\ffmpeg_ext"
set "FF_DIR=%~dp0ffmpeg"

powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12,Tls13'; Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile '!FF_ZIP!' -UseBasicParsing"

if not exist "!FF_ZIP!" (
    echo.
    echo  [ERROR] Could not download FFmpeg.
    echo.
    echo  Please install manually:
    echo    1. Go to https://ffmpeg.org/download.html
    echo    2. Download the Windows static build
    echo    3. Copy ffmpeg.exe into the "ffmpeg" folder next to this file
    echo    4. Re-run this file
    echo.
    pause
    exit /b 1
)

echo       Extracting...
if exist "!FF_TMP!" rmdir /s /q "!FF_TMP!" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '!FF_ZIP!' -DestinationPath '!FF_TMP!' -Force"
del "!FF_ZIP!" >nul 2>&1

if not exist "!FF_DIR!" mkdir "!FF_DIR!"
for /r "!FF_TMP!" %%F in (ffmpeg.exe ffprobe.exe ffplay.exe) do (
    if exist "%%F" copy /y "%%F" "!FF_DIR!\" >nul 2>&1
)
rmdir /s /q "!FF_TMP!" >nul 2>&1

if not exist "!FF_DIR!\ffmpeg.exe" (
    echo.
    echo  [ERROR] FFmpeg extraction failed. Please install manually.
    echo.
    pause
    exit /b 1
)
set "PATH=!FF_DIR!;%PATH%"
echo  [OK] FFmpeg installed to ffmpeg\ folder

:: =========================================================
::  STEP 3: Install Python packages
:: =========================================================
:INSTALL_DEPS
echo.
echo  [->] Installing/updating Python packages (first run may take 2-5 min)...
!PYTHON! -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo  [!!] Some packages failed to install. Continuing anyway...
) else (
    echo  [OK] Packages ready
)

:: =========================================================
::  STEP 4: Start server
:: =========================================================
echo.
echo  ================================================
echo   Server: http://127.0.0.1:5000
echo   Browser will open automatically.
echo   Close this window or press Ctrl+C to stop.
echo  ================================================
echo.
timeout /t 1 /nobreak >nul
start "" "http://127.0.0.1:5000"
!PYTHON! server.py

echo.
echo  Server stopped.
pause
