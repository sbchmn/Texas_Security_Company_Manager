@echo off
REM Hermetic (sqlite) management command. Same DOTENV_PATH trick as runtests.cmd: the developer .env
REM names Redis and ClamAV hosts that are not reachable from here.
setlocal
cd /d "%~dp0..\.."
set DOTENV_PATH=%TEMP%\tscm-no-such-dotenv
".venv\Scripts\python.exe" manage.py %*
endlocal
