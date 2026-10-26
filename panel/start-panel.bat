@echo off
rem start-panel.bat -- ASCII only (cmd reads .bat with the console code page)
rem
rem Logic: if the panel is already listening, just open it in the browser.
rem Otherwise start it in the background (pythonw = no console window).
rem This makes double-clicking the shortcut idempotent: no duplicate instances.
rem
rem NOTE: the probe URL is hardcoded to port 8790. If you change panel_port
rem in config.json, change it here too (start-panel.vbs uses the same value).
setlocal EnableExtensions
set "HERE=%~dp0"
set "PANEL_URL=http://127.0.0.1:8790/"
set "PYW=%HERE%..\.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%HERE%..\python\pythonw.exe"
if not exist "%PYW%" set "PYW=%HERE%..\.venv\Scripts\python.exe"

rem ---- already running? open the browser and do nothing else ----
curl.exe -s -m 2 -o nul "%PANEL_URL%api/status" >nul 2>nul
if not errorlevel 1 goto open_only

rem ---- not running: spawn it headless ----
if not exist "%PYW%" goto nopy
start "" "%PYW%" "%HERE%wb_control_panel.py"
rem the panel opens the browser itself (config.json: open_browser)
exit /b 0

:open_only
start "" "%PANEL_URL%"
exit /b 0

:nopy
echo [ERROR] Python runtime not found. Create the venv first:
echo     python -m venv "%~dp0..\.venv"
pause
exit /b 1
