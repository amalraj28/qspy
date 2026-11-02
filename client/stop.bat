@echo off
setlocal

echo ================================================
echo Stopping custom proxy and clearing environment
echo ================================================


REM Kill the proxy window we started (works even if minimized)
taskkill /FI "WINDOWTITLE eq CustomMITMProxy*" /F >nul 2>&1
if %errorlevel% equ 0 (
    echo Proxy process terminated.
) else (
    echo No running "CustomMITMProxy" window found (may already be closed).
)

REM Also try to kill any python process that might still be holding the port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8888 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM Clear environment variables in this window
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "REQUESTS_CA_BUNDLE="
set "SSL_CERT_FILE="

echo.
echo Environment variables cleared.
echo.
echo ================================================
echo Cleanup complete
echo ================================================
echo - Custom proxy stopped
echo - Proxy environment variables cleared
echo - Network traffic will now go direct
echo ================================================
echo.
pause