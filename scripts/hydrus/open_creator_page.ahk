; Hydrus Creator Search ホットキー (AutoHotkey v1)
;
; Hydrusウィンドウで F を押すと Python スクリプトを起動。
; ページ作成はPython側で行う。

#NoEnv
#SingleInstance Force
SendMode Input
SetTitleMatchMode, 2

SCRIPT_PATH := A_ScriptDir . "\open_creator_page.py"

#IfWinActive, hydrus client

F::
    Run, cmd /K python "%SCRIPT_PATH%"
    return

#IfWinActive
