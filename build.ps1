# 一键构建 PanShare 镜像（Windows / PowerShell 版）
#
#   .\build.ps1                                   # 本机架构构建
#   .\build.ps1 -Output panshare.tar              # 额外导出 tar，供离线机器 docker load
#   .\build.ps1 -Platform linux/amd64              # 指定架构（NAS 多为 amd64 / arm64）
#   .\build.ps1 -Platform linux/amd64,linux/arm64 -Push -Image user/panshare:latest
#   .\build.ps1 -PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
#   .\build.ps1 -BaseImage docker.m.daocloud.io/library/python:3.12-slim   # 拉不到 docker.io 时
[CmdletBinding()]
param(
    [string]$Image = "panshare:latest",
    [string]$Platform = "",
    [string]$Output = "",
    [switch]$Push,
    [string]$PipIndexUrl = "",
    [string]$BaseImage = "",
    [switch]$NoCache
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "X 找不到 docker 命令。" -ForegroundColor Red
    Write-Host "  1) 安装 Docker Desktop 后重跑本脚本；"
    Write-Host "  2) 或把项目推到 GitHub，用 .github/workflows/docker.yml 云构建（含可下载的镜像 tar）。"
    exit 1
}

$buildx = $false
try { docker buildx version | Out-Null; $buildx = $true } catch { $buildx = $false }

$multi = $Platform -like "*,*"
if ($multi -and -not $Push -and -not $Output) {
    Write-Host "X 多架构构建无法导入本地镜像库，请加 -Push，或用 -Output 导出 tar。" -ForegroundColor Red
    exit 1
}

$buildArgs = @()
if ($PipIndexUrl) { $buildArgs += @("--build-arg", "PIP_INDEX_URL=$PipIndexUrl") }
if ($BaseImage) { $buildArgs += @("--build-arg", "BASE_IMAGE=$BaseImage") }
if ($NoCache) { $buildArgs += "--no-cache" }

Write-Host "-> 镜像: $Image"
if ($Platform) { Write-Host "-> 平台: $Platform" }
if ($Output) { Write-Host "-> 导出: $Output" }

if ($buildx -and $multi) {
    $plat = $Platform
    $argv = @("buildx", "build", "--platform", $plat, "--tag", $Image, "--push") + $buildArgs + @(".")
    & docker @argv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
elseif ($buildx -and $Output -and -not $Push) {
    $plat = $Platform
    if (-not $plat) { $plat = "linux/amd64" }
    $argv = @("buildx", "build", "--platform", $plat, "--tag", $Image,
              "--output", "type=docker,dest=$Output", "--provenance=false", "--sbom=false") +
            $buildArgs + @(".")
    & docker @argv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "OK 已导出: $Output（目标机器: docker load -i $Output）" -ForegroundColor Green
}
else {
    $argv = @("build")
    if ($Platform) { $argv += @("--platform", $Platform) }
    $argv += @("--tag", $Image) + $buildArgs + @(".")
    & docker @argv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if ($Push) {
        & docker push $Image
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    if ($Output) {
        & docker save -o $Output $Image
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Write-Host "OK 已导出: $Output（目标机器: docker load -i $Output）" -ForegroundColor Green
    }
}

Write-Host "OK 完成: $Image" -ForegroundColor Green
