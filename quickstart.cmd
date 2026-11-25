@echo off
setlocal
cd /d "%~dp0"
title AzurPromilia Switcher - Quick Start

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo.
    echo [ERROR] Python not found.
    echo         Install Python 3.10+ and enable "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

%PY% -m azp --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Cannot start azp. Run this file from the project folder.
    echo         Current folder: %CD%
    echo.
    pause
    exit /b 1
)

set "HAS_AES="
%PY% -c "import Crypto" >nul 2>nul && set "HAS_AES=1"
if not defined HAS_AES (
    where openssl >nul 2>nul && set "HAS_AES=1"
)
if not defined HAS_AES (
    echo [SETUP] No AES backend found, installing pycryptodome ...
    %PY% -m pip install pycryptodome
    if errorlevel 1 (
        echo [SETUP] Retrying with mirror ...
        %PY% -m pip install pycryptodome -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
    %PY% -c "import Crypto" >nul 2>nul && set "HAS_AES=1"
)
if not defined HAS_AES (
    echo.
    echo [ERROR] Need either pycryptodome or openssl.
    echo         - pip install pycryptodome
    echo         - or install Git for Windows / OpenSSL and add it to PATH
    echo.
    pause
    exit /b 1
)

%PY% -m azp quickstart %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo [EXIT] code %RC%
pause
endlocal
