@echo off
chcp 65001 > nul

echo ================================================
echo  BusAuto - Google Ads Token Refresh
echo ================================================
echo.
echo Step 1: Browser will open - allow Google Ads access
echo Step 2: Copy new refresh_token from console output
echo Step 3: Paste into mcp-google-ads\google-ads.yaml
echo         (line: refresh_token: YOUR_NEW_TOKEN)
echo Step 4: Add to GitHub Secret: GADS_REFRESH_TOKEN
echo.
pause

set "SCRIPT_DIR=%~dp0"
set "VENV_PYTHON=%SCRIPT_DIR%mcp-google-ads\.venv\Scripts\python.exe"
set "TOKEN_SCRIPT=%SCRIPT_DIR%mcp-google-ads\generate_refresh_token.py"
set "CREDENTIALS=%SCRIPT_DIR%mcp-google-ads\credentials.json"

echo Running: %VENV_PYTHON%
echo Script:  %TOKEN_SCRIPT%
echo Creds:   %CREDENTIALS%
echo.

"%VENV_PYTHON%" "%TOKEN_SCRIPT%" -c "%CREDENTIALS%"

echo.
echo ================================================
echo  Done! Copy the refresh_token printed above.
echo  Paste it into:
echo  1) mcp-google-ads\google-ads.yaml
echo  2) GitHub Secrets - GADS_REFRESH_TOKEN
echo ================================================
pause
