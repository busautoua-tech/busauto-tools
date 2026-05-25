@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Захист: якщо запущено Task Scheduler (без флага RUN_MANUAL=1) - виходимо мовчки
rem Для ручного запуску: set RUN_MANUAL=1 && run_full_refresh.bat
if not defined RUN_MANUAL exit /b 0

echo.
echo ============================================================
echo  Step 1/2: Export data from Odoo
echo ============================================================
echo.
python odoo_export.py
if errorlevel 1 goto :err
echo.
echo ============================================================
echo  Step 2/2: Build dashboard HTML
echo ============================================================
echo.
python build_dashboard.py
if errorlevel 1 goto :err
echo.
echo ============================================================
echo  Done. Open: busauto_owner_dashboard.html
echo ============================================================
pause
exit /b 0

:err
echo.
echo [ERROR] Something failed. See odoo_export.log for details.
pause
exit /b 1
