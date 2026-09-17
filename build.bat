@echo off
REM ---------------------------------------------------------------
REM  Build script for yangvis (Windows)
REM  1) Build the frontend bundle
REM  2) Build the Docker image (multi-stage)
REM ---------------------------------------------------------------
setlocal ENABLEDELAYEDEXPANSION

if "%IMAGE_TAG%"=="" set IMAGE_TAG=yangvis:latest

echo ==^> [1/2] Building frontend...
pushd "%~dp0frontend"
if exist package-lock.json (
  call npm ci
) else (
  call npm install
)
if errorlevel 1 goto :err
call npm run build
if errorlevel 1 goto :err
popd

echo ==^> [2/2] Building Docker image (%IMAGE_TAG%)...
docker build -t "%IMAGE_TAG%" .
if errorlevel 1 goto :err

echo ==^> Done. Image: %IMAGE_TAG%
echo     Run with: docker run -d -p 18099:18099 %IMAGE_TAG%
echo     Or:       docker-compose up -d --build
goto :eof

:err
echo Build failed.
exit /b 1
