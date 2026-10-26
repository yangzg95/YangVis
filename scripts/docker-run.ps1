<#
.SYNOPSIS
    裸 docker 方式构建并启动 yangvis（不含 MySQL / Qdrant，需自行准备）。

.EXAMPLE
    # 默认：使用 backend\.env，镜像 luke.yang/docker/yangvis:latest，容器名 yangvis
    .\scripts\docker-run.ps1

.EXAMPLE
    # 指定其他 env 文件和端口
    .\scripts\docker-run.ps1 -EnvFile .env.prod -Port 8080

.EXAMPLE
    # 跳过构建，只用已有镜像重启容器
    .\scripts\docker-run.ps1 -SkipBuild

.EXAMPLE
    # 只构建镜像，不启动容器
    .\scripts\docker-run.ps1 -BuildOnly

.EXAMPLE
    # 构建并推送到镜像仓库（需先 docker login）
    .\scripts\docker-run.ps1 -BuildOnly -Push
#>
param(
    [string]$EnvFile = "backend\.env",
    [string]$ImageName = "docker.cnb.cool/luke.yang/docker/yangvis:latest",
    [string]$ContainerName = "yangvis",
    [int]$Port = 18099,
    # 容器内存硬限制（MB）。1G 小内存服务器建议 640-768，并配少量 worker。
    [int]$MemoryMB = 768,
    # gunicorn worker 数。langchain 较重，单个 worker 常驻 200MB+，1G 机器用 1-2。
    [int]$Workers = 2,
    [switch]$SkipBuild,
    # 只构建（可选推送），不启动容器。
    [switch]$BuildOnly,
    # 构建成功后 docker push 到 ImageName 指定的仓库（需先 docker login）。
    [switch]$Push
)

$ErrorActionPreference = "Stop"

# 切到项目根（脚本位于 scripts\ 下）
Set-Location (Resolve-Path (Join-Path $PSScriptRoot '..'))

# 1. 构建镜像（BuildOnly 模式下不检查 env 文件——构建不需要它）
if (-not $SkipBuild) {
    Write-Host "==> 构建镜像 $ImageName ..." -ForegroundColor Cyan
    docker build -t $ImageName .
    if ($LASTEXITCODE -ne 0) { Write-Error "docker build 失败"; exit $LASTEXITCODE }
}

# 2. 推送镜像（可选）
if ($Push) {
    Write-Host "==> 推送镜像 $ImageName ..." -ForegroundColor Cyan
    docker push $ImageName
    if ($LASTEXITCODE -ne 0) { Write-Error "docker push 失败（先 docker login？）"; exit $LASTEXITCODE }
}

if ($BuildOnly) {
    Write-Host "==> 完成（BuildOnly，未启动容器）" -ForegroundColor Green
    exit 0
}

# 3. 检查 env 文件
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

# 用户上传的资产（幻灯片配图等）写在容器内的 uploads 目录：镜像里的 /app 是构建产物，
# 重建即覆盖，不挂卷的话每次重新部署都会把它们清掉。挂载路径按 backend/app/config.py
# 的口径来——.env 里设了 UPLOAD_MEDIA_DIR 就用它，否则是镜像默认的 /app/uploads。
$uploadsPath = if ($envContent -match "(?m)^UPLOAD_MEDIA_DIR\s*=\s*(.+?)\s*$") {
    $Matches[1].Trim('"').Trim("'")
} else {
    "/app/uploads"
}
# 镜像只预建并 chown 了 /app/uploads（见 Dockerfile）：挂到别的空路径时，命名卷会以
# root 属主创建，而容器以 yangvis 运行，每次上传都会 EACCES。
if ($uploadsPath -ne "/app/uploads") {
    Write-Warning "UPLOAD_MEDIA_DIR=$uploadsPath 不是镜像预建的 /app/uploads，命名卷会以 root 属主创建，上传可能 EACCES。"
}

# 4. 停掉并删除旧容器（存在才删）
$existing = docker ps -aq -f "name=^$ContainerName$"
if ($existing) {
    Write-Host "==> 移除旧容器 $ContainerName ..." -ForegroundColor Cyan
    docker rm -f $ContainerName | Out-Null
}

# 5. 启动新容器
Write-Host "==> 启动容器 $ContainerName (端口 ${Port}:18099, 上传卷 ${ContainerName}_uploads:$uploadsPath) ..." -ForegroundColor Cyan
# --memory-swap 与 --memory 相同 = 禁用 swap，超内存直接 OOM kill（由 restart 策略拉起），
# 避免在小内存机器上因 swap 拖垮整机。
docker run -d `
    --name $ContainerName `
    --restart unless-stopped `
    --memory "${MemoryMB}m" `
    --memory-swap "${MemoryMB}m" `
    -e GUNICORN_WORKERS=$Workers `
    --env-file $EnvFile `
    -v "${ContainerName}_uploads:$uploadsPath" `
    -p "${Port}:18099" `
    $ImageName
if ($LASTEXITCODE -ne 0) { Write-Error "docker run 失败"; exit $LASTEXITCODE }

# 6. 健康检查（最多等 60 秒）
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
    Write-Host "==> 启动成功: http://localhost:$Port" -ForegroundColor Green
} else {
    Write-Warning "健康检查未通过，查看日志: docker logs -f $ContainerName"
}
