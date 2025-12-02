@echo off
setlocal EnableDelayedExpansion

REM ============================================
REM Configuration - replace these placeholders before running
REM ============================================
SET "SCRIPT_URL=http://SERVER_IP_ADDRESS:SERVER_PORT/path/to/web_monitor.py"
SET "SCRIPT_NAME=web_monitor.py"
SET "MITMPROXY_PORT=MITMPROXY_PORT"

REM ============================================
REM Step 0: Install dependencies
REM ============================================
echo ================================================
echo Step 0: Installing dependencies...
echo ================================================
echo.

REM Check if pip is available
where pip >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: pip is not available. Please install Python first.
    echo Download Python from: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Checking/Installing mitmproxy...
pip show mitmproxy >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing mitmproxy...
    pip install mitmproxy
    if %errorlevel% neq 0 (
        echo ERROR: Failed to install mitmproxy
        pause
        exit /b 1
    )
    echo mitmproxy installed successfully!
) else (
    echo mitmproxy is already installed
)
echo.

echo Dependencies installation complete!
echo.

REM ============================================
REM Step 1: Download the mitmproxy script
REM ============================================
echo ================================================
echo Step 1: Downloading mitmproxy script...
echo ================================================
echo URL: %SCRIPT_URL%
echo.

REM Check if curl is available (Windows 10+)
where curl >nul 2>&1
if %errorlevel% equ 0 (
    echo Using curl to download...
    curl -fL -o "%SCRIPT_NAME%" "%SCRIPT_URL%"
) else (
    REM Fallback to PowerShell
    echo Using PowerShell to download...
    powershell -Command "Invoke-WebRequest -Uri '%SCRIPT_URL%' -OutFile '%SCRIPT_NAME%'"
)

if not exist "%SCRIPT_NAME%" (
    echo ERROR: Failed to download script from %SCRIPT_URL%
    pause
    exit /b 1
)

echo Download complete: %SCRIPT_NAME%
echo.

REM ============================================
REM Step 2: Start mitmproxy with the script
REM ============================================
echo ================================================
echo Step 2: Starting mitmproxy...
echo ================================================
echo Running: mitmproxy -s %SCRIPT_NAME% -p %MITMPROXY_PORT%
echo.

REM Start mitmproxy in a new window
start "mitmproxy" mitmproxy -s "%SCRIPT_NAME%" -p %MITMPROXY_PORT% --set termlog_verbosity=info

REM Wait for mitmproxy to start
echo Waiting for mitmproxy to initialize...
timeout /t 5 /nobreak >nul

REM Verify mitmproxy is running
netstat -an | find "%MITMPROXY_PORT%" | find "LISTENING" >nul
if errorlevel 1 (
    echo WARNING: Could not confirm mitmproxy is listening on port %MITMPROXY_PORT%
    echo Check the mitmproxy window for errors.
    pause
)

echo mitmproxy is running on port %MITMPROXY_PORT%
echo.

REM ============================================
REM Step 3: Enable proxy environment
REM ============================================
echo ================================================
echo Step 3: Enabling proxy environment...
echo ================================================

SET HTTPS_PROXY=http://127.0.0.1:%MITMPROXY_PORT%
SET HTTP_PROXY=http://127.0.0.1:%MITMPROXY_PORT%
SET REQUESTS_CA_BUNDLE=%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem
SET SSL_CERT_FILE=%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem

echo Proxy environment variables set:
echo   HTTP_PROXY=%HTTP_PROXY%
echo   HTTPS_PROXY=%HTTPS_PROXY%
echo   REQUESTS_CA_BUNDLE=%REQUESTS_CA_BUNDLE%
echo   SSL_CERT_FILE=%SSL_CERT_FILE%
echo.

REM ============================================
REM Ready to use
REM ============================================
echo ================================================
echo Setup Complete!
echo ================================================
echo - mitmproxy is running with script: %SCRIPT_NAME%
echo - All traffic will route through mitmproxy
echo - You can now run your applications in this window
echo.
echo To view captured traffic:
echo   - Check the mitmproxy window
echo ================================================
echo.

REM Open a new command prompt with the environment variables set
cmd /k

exit /b 0
