@echo off
cd /d %~dp0
chcp 65001 > nul
title EventMonitor

echo EventMonitor を起動します...

if not exist "venv" (
    echo エラー: 仮想環境^(venv^)が見つかりません。
    echo setup.bat を先に実行してください。
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo.
echo ========================================
echo  EventMonitor - Windows Mode
echo ========================================
echo.

python main.py

if %errorlevel% neq 0 (
    echo.
    echo アプリケーションがエラー終了しました。
    pause
) else (
    echo アプリケーションを終了します。
    timeout /t 3
)
