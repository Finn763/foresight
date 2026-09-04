@echo off
rem ============================================================
rem  Foresight prediction dashboard - one-click launcher
rem
rem  Double-click: opens a terminal window running the web server
rem  (logs visible). Closing that terminal window stops the server
rem  (Windows console close event terminates python).
rem  Default: 127.0.0.1:8765 internal mode.
rem  NOTE: script uses ASCII only - cmd parses .bat as ANSI.
rem  NOTE: project path has no spaces; add quotes if it ever does.
rem ============================================================
title Foresight Dashboard Launcher

set "APP_DIR=%~dp0"
set "PORT=8765"

rem ---- already running? just open the browser ----
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [Foresight] Dashboard already running on port %PORT% - opening browser...
    start http://127.0.0.1:%PORT%
    exit /b 0
)

echo [Foresight] Starting dashboard (http://127.0.0.1:%PORT%)...
rem pop a terminal window running the server in foreground; close window = stop server
start "Foresight Dashboard" cmd /k "cd /d %APP_DIR% && .venv\Scripts\python.exe scripts\web_server.py"

rem ---- wait for the port (max 15s), then open the browser ----
rem (timeout errors suppressed: harmless when PATH has GNU timeout, e.g. from Git Bash)
set "READY="
for /l %%i in (1,1,30) do (
    netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
    if not errorlevel 1 ( set "READY=1" & goto :open )
    timeout /t 1 /nobreak >nul 2>&1
)
:open
if defined READY (
    start http://127.0.0.1:%PORT%
    echo [Foresight] Opened http://127.0.0.1:%PORT% - server logs are in the terminal window.
) else (
    echo [Foresight] Port not ready - check the terminal window for errors; try http://127.0.0.1:%PORT% manually.
)
exit /b 0
