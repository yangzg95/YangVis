<#
.SYNOPSIS
    裸 docker 方式构建并启动 yangvis（不含 MySQL / Qdrant，需自行准备）。

.EXAMPLE
    # 默认：使用 backend\.env，镜像 luke.yang/docker/yangvis:latest，容器名 yangvis
    .\docker-run.ps1

.EXAMPLE
    # 指定其他 env 文件和端口
    .\docker-run.ps1 -EnvFile .env.prod -Port 8080

.EXAMPLE
    # 跳过构建，只用已有镜像重启容器
    .\docker-run.ps1 -SkipBuild
#>
param(
    [string]$EnvFile = "backend\.env",
    [string]$ImageName = "docker.cnb.cool/luke.yang/docker/yangvis:latest",
    [string]$ContainerName = "yangvis",
    [int]$Port = 18099,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

# 切到脚本所在目录（项目根）
Set-Location $PSScriptRoot

# 1. 检查 env 文件
if (-not (Test-Path $EnvFile)) {
    Write-Error "env 文件不存在: $EnvFile"
    exit 1
}
Write-Host "==> 使用 env 文件: $EnvFile" -ForegroundColor Cyan

# 提示：容器内 localhost 指向容器自己，DB/Qdrant 不能填 localhost
$envContent = Get-Content $EnvFile -Raw
if ($envContent -match "(?m)^(DB_HOST|QDRANT_HOST)\s*=\s*(localhost|127\.0\.0\.1)\s*$") {
    Write-Warning "$EnvFile 中 DB_HOST/QDRANT_HOST 是 localhost，容器内无法访问宿主机服务。"
    Write-Warning "如 MySQL/Qdrant 跑在宿主机，请改为 host.docker.internal（Windows Docker Desktop 可用）。"
}

# 2. 构建镜像
if (-not $SkipBuild) {
    Write-Host "==> 构建镜像 $ImageName ..." -ForegroundColor Cyan
    docker build -t $ImageName .
    if ($LASTEXITCODE -ne 0) { Write-Error "docker build 失败"; exit $LASTEXITCODE }
}

# 3. 停掉并删除旧容器（存在才删）
$existing = docker ps -aq -f "name=^$ContainerName$"
if ($existing) {
    Write-Host "==> 移除旧容器 $ContainerName ..." -ForegroundColor Cyan
    docker rm -f $ContainerName | Out-Null
}

# 4. 启动新容器
Write-Host "==> 启动容器 $ContainerName (端口 ${Port}:18099) ..." -ForegroundColor Cyan
docker run -d `
    --name $ContainerName `
    --restart unless-stopped `
    --env-file $EnvFile `
    -p "${Port}:18099" `
    $ImageName
if ($LASTEXITCODE -ne 0) { Write-Error "docker run 失败"; exit $LASTEXITCODE }

# 5. 健康检查（最多等 60 秒）
Write-Host "==> 等待健康检查 ..." -ForegroundColor Cyan
$ok = $false
foreach ($i in 1..12) {
    Start-Sleep -Seconds 5
    $status = docker inspect -f "{{.State.Health.Status}}" $ContainerName 2>$null
    Write-Host "    [$i/12] health: $status"
    if ($status -eq "healthy") { $ok = $true; break }
    if ($status -eq "unhealthy") { break }
}

if ($ok) {
    Write-Host "==> 启动成功: http://localhost:$Port$((Select-String -Path $EnvFile -Pattern '^CONTEXT_PATH=(.+)$' | ForEach-Object { $_.Matches[0].Groups[1].Value }))" -ForegroundColor Green
} else {
    Write-Warning "健康检查未通过，查看日志: docker logs -f $ContainerName"
}
