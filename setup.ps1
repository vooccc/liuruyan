# ========================================
#  柳如烟项目 - 一键初始化脚本
#  适用于：新机器 clone 后首次运行
#  使用方法：右键 → 使用 PowerShell 运行
#           或在项目根目录执行: .\setup.ps1
# ========================================

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir

function Write-Step($num, $text) {
    Write-Host "`n[$num] $text" -ForegroundColor Cyan
}

function Write-Ok($text) {
    Write-Host "  ✓ $text" -ForegroundColor Green
}

function Write-Warn($text) {
    Write-Host "  ⚠ $text" -ForegroundColor Yellow
}

function Write-Fail($text) {
    Write-Host "  ✗ $text" -ForegroundColor Red
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Magenta
Write-Host "  柳如烟项目 - 一键初始化" -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta

# ---- 0. 检查 Python ----
Write-Step "0" "检查 Python 环境"

$pythonCmd = $null
$pythonVersion = $null

foreach ($cmd in @("python", "python3", "C:\Program Files\Python\Python311\python.exe")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+\.\d+)") {
            $v = [version]$Matches[1]
            if ($v -ge [version]"3.10") {
                $pythonCmd = $cmd
                $pythonVersion = $Matches[0]
                break
            }
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Fail "未找到 Python 3.10+，请先安装 Python: https://www.python.org/downloads/"
    Read-Host "按回车退出"
    exit 1
}
Write-Ok "找到 $pythonVersion ($pythonCmd)"

# ---- 1. 创建后端虚拟环境 + 安装依赖 ----
Write-Step "1" "创建后端虚拟环境"

$venvPath = Join-Path $RootDir "backend\venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvPip = Join-Path $venvPath "Scripts\pip.exe"

if (Test-Path $venvPython) {
    Write-Ok "venv 已存在，跳过创建"
} else {
    & $pythonCmd -m venv $venvPath
    if (-not $?) {
        Write-Fail "创建 venv 失败"
        Read-Host "按回车退出"
        exit 1
    }
    Write-Ok "venv 创建成功"
}

Write-Host "  安装 Python 依赖（可能需要1-2分钟）..." -ForegroundColor Gray
& $venvPip install -r (Join-Path $RootDir "backend\requirements.txt") --quiet 2>&1 | Out-Null
if ($?) {
    Write-Ok "Python 依赖安装完成"
} else {
    Write-Warn "部分依赖可能安装失败，尝试不使用缓存重装..."
    & $venvPip install -r (Join-Path $RootDir "backend\requirements.txt") --no-cache-dir
}

# ---- 2. 安装前端依赖 ----
Write-Step "2" "安装前端依赖"

$frontendDir = Join-Path $RootDir "frontend"
if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    Write-Host "  安装 npm 依赖（可能需要2-3分钟）..." -ForegroundColor Gray
    Push-Location $frontendDir
    npm install 2>&1 | Out-Null
    Pop-Location
    if ($?) {
        Write-Ok "前端依赖安装完成"
    } else {
        Write-Fail "前端依赖安装失败，请检查 Node.js 是否已安装"
        Read-Host "按回车退出"
        exit 1
    }
} else {
    Write-Ok "node_modules 已存在，跳过安装"
}

# ---- 3. 检查 Rust 工具链 ----
Write-Step "3" "检查 Rust 工具链（Tauri 依赖）"

$rustInstalled = $false
try {
    $rustVer = & rustc --version 2>&1
    if ($rustVer -match "rustc") {
        $rustInstalled = $true
        Write-Ok "$rustVer"
    }
} catch {}

if (-not $rustInstalled) {
    Write-Warn "未检测到 Rust 工具链"
    Write-Host ""
    Write-Host "  Tauri 桌面应用需要 Rust 编译环境。" -ForegroundColor Yellow
    Write-Host "  推荐安装方式：" -ForegroundColor Yellow
    Write-Host "    1. 访问 https://rustup.rs/" -ForegroundColor White
    Write-Host "    2. 下载并运行 rustup-init.exe" -ForegroundColor White
    Write-Host "    3. 安装完成后重新运行此脚本" -ForegroundColor White
    Write-Host ""
    $installRust = Read-Host "是否现在自动安装 Rust？(y/N)"
    if ($installRust -eq "y" -or $installRust -eq "Y") {
        Write-Host "  正在下载并安装 rustup..." -ForegroundColor Gray
        $rustupUrl = "https://win.rustup.rs/x86_64"
        $rustupFile = Join-Path $env:TEMP "rustup-init.exe"
        Invoke-WebRequest -Uri $rustupUrl -OutFile $rustupFile -UseBasicParsing
        & $rustupFile -y
        # 刷新环境变量
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        try {
            $rustVer = & rustc --version 2>&1
            Write-Ok "Rust 安装成功: $rustVer"
            $rustInstalled = $true
        } catch {
            Write-Warn "Rust 已安装但需要重启终端才能生效"
        }
    }
}

# ---- 4. 创建环境配置文件 ----
Write-Step "4" "创建环境配置文件"

# .env
$envFile = Join-Path $RootDir ".env"
$envExample = Join-Path $RootDir ".env.example"
if (-not (Test-Path $envFile) -and (Test-Path $envExample)) {
    Copy-Item $envExample $envFile
    Write-Ok "已创建 .env（请编辑填入 API Key）"
} elseif (Test-Path $envFile) {
    Write-Ok ".env 已存在"
} else {
    Write-Warn ".env.example 不存在，跳过"
}

# models.secrets.json
$secretsFile = Join-Path $RootDir "models.secrets.json"
$secretsExample = Join-Path $RootDir "models.secrets.json.example"
if (-not (Test-Path $secretsFile) -and (Test-Path $secretsExample)) {
    Copy-Item $secretsExample $secretsFile
    Write-Ok "已创建 models.secrets.json（请编辑填入 API Key）"
} elseif (Test-Path $secretsFile) {
    Write-Ok "models.secrets.json 已存在"
} else {
    Write-Warn "models.secrets.json.example 不存在，跳过"
}

# models.json (从 example 自动恢复，后端启动时也会做)
$modelsFile = Join-Path $RootDir "models.json"
$modelsExample = Join-Path $RootDir "models.json.example"
if (-not (Test-Path $modelsFile) -and (Test-Path $modelsExample)) {
    Copy-Item $modelsExample $modelsFile
    Write-Ok "已从 example 恢复 models.json（内置模型列表）"
} elseif (Test-Path $modelsFile) {
    Write-Ok "models.json 已存在"
}

# ---- 5. 创建运行时目录 ----
Write-Step "5" "创建运行时目录"

$runtimeDirs = @(
    (Join-Path $RootDir "backend\memory\sessions")
    (Join-Path $RootDir "agents\default\memory")
)

foreach ($dir in $runtimeDirs) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Ok "已创建 $dir"
    } else {
        Write-Ok "$dir 已存在"
    }
}

# ---- 6. Bootstrap ripgrep（按当前架构自动下载到 vendor/ripgrep/<arch>/）----
Write-Step "6" "Bootstrap ripgrep 二进制"

$rgScript = Join-Path $RootDir "scripts\bootstrap_ripgrep.py"
if (Test-Path $rgScript) {
    & $venvPython $rgScript
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "ripgrep 已就位（vendor/ripgrep/）"
    } else {
        Write-Warn "ripgrep 自动下载失败——可联网后手动跑 'python scripts/bootstrap_ripgrep.py'"
    }
} else {
    Write-Warn "未找到 $rgScript，跳过"
}

# ---- 完成 ----
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  初始化完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

$hasTodo = $false

if (-not (Test-Path $envFile) -or (Get-Content $envFile -Raw) -match "your-api-key") {
    Write-Host "  📝 请编辑 .env 填入 API Key" -ForegroundColor Yellow
    $hasTodo = $true
}

if (-not (Test-Path $secretsFile) -or (Get-Content $secretsFile -Raw) -match "你的-API-KEY") {
    Write-Host "  📝 请编辑 models.secrets.json 填入模型 API Key" -ForegroundColor Yellow
    $hasTodo = $true
}

if (-not $rustInstalled) {
    Write-Host "  📝 请安装 Rust 工具链后再启动" -ForegroundColor Yellow
    $hasTodo = $true
}

if (-not $hasTodo) {
    Write-Host "  所有准备工作就绪，可以启动了！" -ForegroundColor Green
}

Write-Host ""
Write-Host "  启动方式：" -ForegroundColor White
Write-Host "    cd frontend && npm run tauri dev" -ForegroundColor Gray
Write-Host ""

Read-Host "按回车退出"
