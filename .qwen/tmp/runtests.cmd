@echo off
REM Hermetic (sqlite) test leg. DOTENV_PATH points at a non-existent file so config/settings.py
REM does not load the developer .env, whose REDIS_URL / CLAMAV_HOST name unreachable services.
REM ALLOWED_HOSTS is deliberately NOT set: settings' test fallback is what admits "testserver".
setlocal
cd /d "%~dp0..\.."
set DOTENV_PATH=%TEMP%\tscm-no-such-dotenv
".venv\Scripts\python.exe" manage.py test %* --noinput
endlocal
