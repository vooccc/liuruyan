<#
.SYNOPSIS
    读取 / 修改项目版本号（同步更新 tauri.conf.json、package.json 与 Cargo.toml）。

.PARAMETER Mode
    - print : 只打印当前版本（不改文件）
    - patch : X.Y.Z -> X.Y.(Z+1)  【默认】
    - minor : X.Y.Z -> X.(Y+1).0
    - major : X.Y.Z -> (X+1).0.0
    - none  : 等价于 print
#>

param(
    [ValidateSet("print", "patch", "minor", "major", "none")]
    [string]$Mode = "patch"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TauriConfPath = Join-Path $ProjectRoot "frontend\src-tauri\tauri.conf.json"
$PackageJsonPath = Join-Path $ProjectRoot "frontend\package.json"
$CargoTomlPath = Join-Path $ProjectRoot "frontend\src-tauri\Cargo.toml"

if (-not (Test-Path $TauriConfPath)) {
    Write-Error "未找到 tauri.conf.json: $TauriConfPath"
    exit 1
}

$content = Get-Content $TauriConfPath -Raw -Encoding UTF8

if ($content -notmatch '"version"\s*:\s*"(\d+)\.(\d+)\.(\d+)"') {
    Write-Error "tauri.conf.json 里没找到符合 semver 的 version 字段"
    exit 1
}

$major = [int]$matches[1]
$minor = [int]$matches[2]
$patch = [int]$matches[3]
$oldVer = "$major.$minor.$patch"

switch ($Mode) {
    "patch" { $patch++ }
    "minor" { $minor++; $patch = 0 }
    "major" { $major++; $minor = 0; $patch = 0 }
    default { }
}

$newVer = "$major.$minor.$patch"

if ($Mode -in @("print", "none")) {
    Write-Host "当前版本: $oldVer"
    exit 0
}

if ($oldVer -eq $newVer) {
    Write-Host "版本未变: $oldVer"
    exit 0
}

# 1. 更新 tauri.conf.json
$newTauriContent = $content -replace '"version"\s*:\s*"\d+\.\d+\.\d+"', "`"version`": `"$newVer`""
[System.IO.File]::WriteAllText($TauriConfPath, $newTauriContent, (New-Object System.Text.UTF8Encoding $false))

# 2. 同步更新 frontend/package.json
if (Test-Path $PackageJsonPath) {
    $pkgContent = Get-Content $PackageJsonPath -Raw -Encoding UTF8
    if ($pkgContent -match '"version"\s*:\s*"\d+\.\d+\.\d+"') {
        $newPkgContent = $pkgContent -replace '"version"\s*:\s*"\d+\.\d+\.\d+"', "`"version`": `"$newVer`""
        [System.IO.File]::WriteAllText($PackageJsonPath, $newPkgContent, (New-Object System.Text.UTF8Encoding $false))
    }
}

# 3. 同步更新 Cargo.toml
if (Test-Path $CargoTomlPath) {
    $cargoContent = Get-Content $CargoTomlPath -Raw -Encoding UTF8
    if ($cargoContent -match '(?m)^version\s*=\s*"\d+\.\d+\.\d+"') {
        $newCargoContent = $cargoContent -replace '(?m)^version\s*=\s*"\d+\.\d+\.\d+"', "version = `"$newVer`""
        [System.IO.File]::WriteAllText($CargoTomlPath, $newCargoContent, (New-Object System.Text.UTF8Encoding $false))
    }
}

Write-Host "版本递增: $oldVer -> $newVer"
