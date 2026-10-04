@echo off
REM Throwaway MySQL 8.4 + `manage.py migrate`, in one shot, for the live migration leg of the standing
REM gates. The root password is read out of .qwen/tmp/liveenv.cmd rather than typed here, so this script
REM cannot drift from the harness and no secret is invented for it. The port is published on 127.0.0.1
REM only, the container is recreated on the way in, and it holds nothing worth keeping afterwards.
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

set PWFILE=.qwen\tmp\liveenv.cmd
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b "set MYSQL_PASSWORD=" "%PWFILE%"`) do set PW=%%b
if "%PW%"=="" (
  echo could not read MYSQL_PASSWORD from %PWFILE%
  exit /b 1
)

docker rm -f tscm-mysql-54 >nul 2>&1
docker run -d --name tscm-mysql-54 -e MYSQL_ROOT_PASSWORD=!PW! -e MYSQL_DATABASE=tscm ^
  -p 127.0.0.1:13306:3306 mysql:8.4 || exit /b 1

set DOTENV_PATH=%TEMP%\tscm-no-such-dotenv
set MYSQL_HOST=127.0.0.1
set MYSQL_PORT=13306
set MYSQL_DATABASE=tscm
set MYSQL_USER=root
set MYSQL_PASSWORD=!PW!

set /a tries=0
:wait
set /a tries+=1
if %tries% gtr 60 (
  echo mysql did not answer a real query within 180s
  exit /b 1
)
timeout /t 3 /nobreak >nul
REM `mysqladmin ping` is not a readiness test: it exits 0 when the server refuses the login, and during
REM initialization the temporary server answers at all — both look like "ready" and then the migrate
REM dies mid-handshake. Ask for an actual result set instead.
docker exec tscm-mysql-54 mysql -uroot -p!PW! -e "select 1" >nul 2>&1
if errorlevel 1 goto wait

".venv\Scripts\python.exe" manage.py migrate
echo --- migrate exit %ERRORLEVEL% ---
".venv\Scripts\python.exe" manage.py showmigrations core 2>nul | find /c "[X]"
".venv\Scripts\python.exe" .qwen\tmp\probe_mysql_slice.py
echo --- probe exit %ERRORLEVEL% ---
