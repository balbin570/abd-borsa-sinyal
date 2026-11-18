@echo off
cd /d "%~dp0"
if not exist "ibkr_bridge\\local_runtime" mkdir "ibkr_bridge\\local_runtime"
:restart
py -3 -m ibkr_bridge.home_server --interval 300
echo Home server exited; retrying in 30 seconds...
timeout /t 30 /nobreak >nul
goto restart
