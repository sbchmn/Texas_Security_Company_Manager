@echo off
REM Same environment as livetest.cmd, but running an arbitrary python file instead of manage.py.
setlocal
cd /d "%~dp0..\.."
set DOTENV_PATH=%TEMP%\tscm-no-such-dotenv
set TEST_LIVE_SERVICES=true
set MYSQL_HOST=127.0.0.1
set MYSQL_PORT=13306
set MYSQL_DATABASE=tscm
set MYSQL_USER=root
set MYSQL_PASSWORD=ci-root-password
set REDIS_URL=redis://127.0.0.1:16379/0
".venv\Scripts\python.exe" %*
endlocal
