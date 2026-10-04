@echo off
REM Live-services leg against the throwaway mysql:8.4 + redis:7.4 containers (13306 / 16379).
REM This is the leg that proves the migration-installed tenant triggers, which sqlite skips.
REM MYSQL_USER=root: the unprivileged tscm user cannot create the test_tscm database.
REM DOTENV_PATH points at a non-existent file so config/settings.py ignores the developer .env,
REM whose CLAMAV_HOST / REDIS_URL name compose services that are not reachable from the host.
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
".venv\Scripts\python.exe" manage.py %*
endlocal
