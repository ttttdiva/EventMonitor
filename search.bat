@echo off
cd /d %~dp0
chcp 65001 > nul

if not exist "venv\Scripts\python.exe" (
    echo エラー: 仮想環境^(venv^)が見つかりません。
    echo setup.bat を先に実行してください。
    exit /b 1
)

venv\Scripts\python.exe -X utf8 scripts\util\search_tweets.py %*
