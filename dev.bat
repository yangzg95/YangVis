@echo off
REM ---------------------------------------------------------------
REM  Dev start script for yangvis (Windows)
REM    dev.bat          start backend + frontend
REM    dev.bat backend  start backend only
REM    dev.bat frontend start frontend only
REM  Backend : http://localhost:18099  (docs: /api/docs)
REM  Frontend: http://localhost:5173   (/api proxied to backend)
REM ---------------------------------------------------------------
setlocal

set ROOT=%~dp0
set TARGET=%1
if "%TARGET%"=="" set TARGET=all

if not exist "%ROOT%backend\.env" (
  echo ==^> backend\.env not found, copying from .env.example
  copy /y "%ROOT%backend\.env.example" "%ROOT%backend\.env" >nul
  echo     Please fill in __CHANGE_ME__ values in backend\.env
)

if "%TARGET%"=="frontend" goto :frontend

echo ==^> Syncing python dependencies (uv sync)...
pushd "%ROOT%"
call uv sync
if errorlevel 1 goto :err
popd

if "%TARGET%"=="backend" (
  echo ==^> Starting backend on http://localhost:18099
  pushd "%ROOT%backend"
  call uv run --project "%ROOT%" uvicorn app.main:app --host 0.0.0.0 --port 18099 --reload
  popd
  goto :eof
)

echo ==^> Starting backend in a new window...
start "yangvis backend" cmd /k "cd /d %ROOT%backend && uv run --project %ROOT% uvicorn app.main:app --host 0.0.0.0 --port 18099 --reload"

:frontend
echo ==^> Installing frontend dependencies...
pushd "%ROOT%frontend"
if not exist node_modules (
  if exist package-lock.json ( call npm ci ) else ( call npm install )
  if errorlevel 1 goto :err
)
echo ==^> Starting frontend on http://localhost:5173
call npm run dev
popd
goto :eof

:err
echo Startup failed.
exit /b 1
