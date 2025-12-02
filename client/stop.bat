@echo off
echo ================================================
echo Stopping mitmproxy background process...
echo ================================================
echo.

REM Kill mitmdump process
taskkill /F /IM mitmproxy.exe >nul 2>&1

if %errorlevel% equ 0 (
    echo Successfully stopped mitmproxy
) else (
    echo mitmproxy was not running
)

echo.
echo ================================================
echo Clearing proxy environment variables...
echo ================================================
echo.

REM Clear proxy environment variables
SET HTTPS_PROXY=
SET HTTP_PROXY=
SET REQUESTS_CA_BUNDLE=
SET SSL_CERT_FILE=

echo Environment variables cleared:
echo   HTTP_PROXY (cleared)
echo   HTTPS_PROXY (cleared)
echo   REQUESTS_CA_BUNDLE (cleared)
echo   SSL_CERT_FILE (cleared)

echo.
echo ================================================
echo Cleanup Complete!
echo ================================================
echo - mitmproxy stopped
echo - Proxy environment variables cleared
echo - Network traffic will now go direct
echo ================================================
echo.
echo Done!
pause