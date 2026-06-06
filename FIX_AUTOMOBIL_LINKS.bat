@echo off
chcp 65001 >nul
title Fix automobil.in.ua в описах товарів

echo ============================================================
echo  Видалення посилань на automobil.in.ua з описів товарів
echo ============================================================
echo.
echo  [T] - Тест (1 товар, без збереження)
echo  [F] - Повне виправлення всіх товарів
echo  [ESC/Enter] - Вихід
echo.
choice /c TFE /n /m "Вибір (T/F/E): "

if errorlevel 3 goto :exit
if errorlevel 2 goto :full
if errorlevel 1 goto :test

:test
echo.
echo >>> ТЕСТОВИЙ РЕЖИМ (1 товар, без збереження)
echo.
python fix_automobil_links.py
goto :done

:full
echo.
echo >>> УВАГА! Буде виправлено ВСІ знайдені товари!
echo.
choice /c YN /n /m "Ви впевнені? (Y/N): "
if errorlevel 2 goto :exit
echo.
echo >>> ПОВНИЙ ЗАПУСК...
echo.
python fix_automobil_links.py --full
goto :done

:done
echo.
pause

:exit
