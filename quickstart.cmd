@echo off
rem 双击入口：检查 Python 与 AES 后端，再交给 azp quickstart 向导。
setlocal
cd /d "%~dp0"
set "AZP_PYCRYPTODOME_VERSION=4.4.0"

rem 用 where 解析出的完整路径调解释器，避免 PATH 里同名程序被先执行。
for /f "delims=" %%I in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%I"
if not defined PYEXE (
    echo [错误] 找不到 python。请安装 Python 3.10+，安装时勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)

"%PYEXE%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] Python 版本过低，需要 3.10 或更高。
    "%PYEXE%" --version
    pause
    exit /b 1
)

rem AES 后端：优先 pycryptodome，其次系统 openssl；都没有才按固定版本安装。
"%PYEXE%" -c "import Crypto" >nul 2>nul
if errorlevel 1 (
    where openssl >nul 2>nul
    if errorlevel 1 (
        echo [准备] 未找到 AES 后端，正在安装 pycryptodome==%AZP_PYCRYPTODOME_VERSION% ...
        "%PYEXE%" -m pip install --disable-pip-version-check "pycryptodome==%AZP_PYCRYPTODOME_VERSION%"
        if errorlevel 1 (
            echo [错误] pycryptodome 安装失败。也可以安装 OpenSSL 并加入 PATH 后重试。
            pause
            exit /b 1
        )
    )
)

rem %* 必须原样透传（整体加引号会把多个参数并成一个），向导内部再逐个解析。
"%PYEXE%" -m azp quickstart %*
set "AZP_EXIT=%ERRORLEVEL%"
if not "%AZP_EXIT%"=="0" (
    echo.
    echo [退出] 向导返回错误码 %AZP_EXIT%。可把上方输出贴给维护者排查。
)
pause
exit /b %AZP_EXIT%
