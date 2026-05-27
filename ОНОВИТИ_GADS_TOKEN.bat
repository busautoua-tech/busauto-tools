@echo off
chcp 65001 > nul
echo ================================================
echo  BusAuto — Оновлення Google Ads refresh token
echo ================================================
echo.
echo Крок 1: Відкриється браузер → дозволь доступ до Google Ads
echo Крок 2: Скопіюй новий refresh_token з консолі
echo Крок 3: Встав його в mcp-google-ads\google-ads.yaml
echo         (рядок: refresh_token: ВАШ_НОВИЙ_ТОКЕН)
echo Крок 4: Додай також як GitHub Secret: GADS_REFRESH_TOKEN
echo.
pause

cd /d "%~dp0mcp-google-ads"
.venv\Scripts\python.exe generate_refresh_token.py -c credentials.json

echo.
echo ================================================
echo  Готово! Скопіюй refresh_token вище і встав у:
echo  mcp-google-ads\google-ads.yaml (рядок refresh_token)
echo  Та в GitHub Secrets: Settings - Secrets - GADS_REFRESH_TOKEN
echo ================================================
pause
