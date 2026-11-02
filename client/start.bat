@echo off
setlocal EnableDelayedExpansion

REM ============================================
REM Configuration - EDIT THESE
REM ============================================
set "SERVER_IP=127.0.0.1"
set "SERVER_PORT=5000"
set "BUNDLE_URL=http://%SERVER_IP%:%SERVER_PORT%/static/custom_proxy.py"
set "BUNDLE_NAME=custom_proxy.py"

set "PROXY_HOST=127.0.0.1"
set "PROXY_PORT=8888"
set "CERT_DIR=%USERPROFILE%\.custom-proxy"
set "CA_CERT=%CERT_DIR%\ca.crt"


REM ============================================
REM Step 0: Basic checks
REM ============================================
echo ================================================
echo Custom Proxy - Client Setup
echo ================================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not in PATH.
    echo Install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ============================================
REM Step 1: Download the single proxy bundle
REM ============================================
echo Step 1: Downloading proxy bundle...
echo URL: %BUNDLE_URL%
echo.

where curl >nul 2>&1
if %errorlevel% equ 0 (
    curl -fsSL -o "%BUNDLE_NAME%" "%BUNDLE_URL%"
) else (
    powershell -NoProfile -Command ^
        "try { Invoke-WebRequest -Uri '%BUNDLE_URL%' -OutFile '%BUNDLE_NAME%' -UseBasicParsing } catch { exit 1 }"
)

if not exist "%BUNDLE_NAME%" (
    echo [ERROR] Failed to download %BUNDLE_NAME%
    pause
    exit /b 1
)
echo Downloaded: %BUNDLE_NAME%
echo.

REM ============================================
REM Step 2: Start the custom proxy (minimized)
REM ============================================
echo Step 2: Starting custom proxy (minimized window)...
echo.

REM Start minimized with a known window title so stop.bat can find it
start "CustomMITMProxy" /MIN python "%BUNDLE_NAME%" --host %PROXY_HOST% --port %PROXY_PORT% --cert-dir "%CERT_DIR%"

echo Waiting for proxy and CA certificate...
set /a attempts=0

:wait_loop
timeout /t 1 /nobreak >nul
set /a attempts+=1

if exist "%CA_CERT%" (
    netstat -an | findstr ":%PROXY_PORT% " | findstr "LISTENING" >nul 2>&1
    if !errorlevel! equ 0 goto proxy_ready
)

if !attempts! geq 20 (
    echo [ERROR] Proxy did not start or CA was not generated in time.
    echo Check the minimized "CustomMITMProxy" window for errors.
    pause
    exit /b 1
)
goto wait_loop

:proxy_ready
echo Proxy is running on %PROXY_HOST%:%PROXY_PORT%
echo CA certificate : %CA_CERT%
echo.

python -c "import certifi, pathlib; p=pathlib.Path(r'%CERT_DIR%'); (p/'combined-ca.pem').write_bytes(pathlib.Path(certifi.where()).read_bytes() + b'\n' + (p/'ca.crt').read_bytes())"
if errorlevel 1 (
    echo [ERROR] Failed to create combined CA bundle.
    exit /b 1
)

REM ============================================
REM Step 3: Enable proxy environment
REM ============================================
echo Step 3: Enabling proxy environment for this window...
echo.

set "HTTP_PROXY=http://%PROXY_HOST%:%PROXY_PORT%"
set "HTTPS_PROXY=http://%PROXY_HOST%:%PROXY_PORT%"
SET "REQUESTS_CA_BUNDLE=%USERPROFILE%\.custom-proxy\combined-ca.pem"
SET "SSL_CERT_FILE=%USERPROFILE%\.custom-proxy\combined-ca.pem"

echo ========================================
echo  Setup complete
echo ========================================
echo HTTP_PROXY          = %HTTP_PROXY%
echo HTTPS_PROXY         = %HTTPS_PROXY%
echo REQUESTS_CA_BUNDLE  = %REQUESTS_CA_BUNDLE%
echo.
echo The proxy is running minimized (title: CustomMITMProxy).
echo Run your Python scripts from THIS window.
echo When finished, run stop.bat
echo ========================================
echo.

cmd /k
