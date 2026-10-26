@echo off
REM ---------------------------------------------------------------
REM  Build script for yangvis (Windows)
REM  Builds the multi-stage Docker image only. The frontend bundle is
REM  built inside the image (frontend-builder stage); running npm build
REM  on the host would be wasted work - frontend/dist is excluded by
REM  .dockerignore and never reaches the build context.
REM    set IMAGE_TAG=my:tag ^& scripts\build.bat
REM ---------------------------------------------------------------
setlocal

REM 脚本位于 scripts\，构建上下文（Dockerfile 所在处）是项目根
pushd "%~dp0.."
set ROOT=%CD%
popd

if "%IMAGE_TAG%"=="" set IMAGE_TAG=yangvis:latest

echo ==^> Building Docker image (%IMAGE_TAG%)...
docker build -t "%IMAGE_TAG%" "%ROOT%"
if errorlevel 1 goto :err

echo ==^> Done. Image: %IMAGE_TAG%
echo     Run with: docker run -d -p 18099:18099 %IMAGE_TAG%
echo     Or:       docker-compose up -d --build
goto :eof

:err
echo Build failed.
exit /b 1
