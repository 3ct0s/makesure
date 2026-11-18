# Build MakeSureAgent.exe on Windows (also used by the GitHub Actions runner).
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 [-Console]
param(
    [switch]$Console,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Venv = Join-Path $Root ".venv-build"
if (-not (Test-Path $Venv)) {
    & $Python -m venv $Venv
}
$Py = Join-Path $Venv "Scripts\python.exe"

& $Py -m pip install --upgrade pip
& $Py -m pip install -r requirements.txt "pyinstaller>=6.10"
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

$SpecArgs = @("packaging\agent.spec", "--noconfirm", "--clean")
if ($Console) { $SpecArgs += @("--", "--console") }
& $Py -m PyInstaller @SpecArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Name = if ($Console) { "MakeSureAgentDebug.exe" } else { "MakeSureAgent.exe" }
$Exe = Join-Path $Root "dist\$Name"
if (-not (Test-Path $Exe)) { throw "expected $Exe was not produced" }
$SizeMb = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Write-Host "Built $Exe ($SizeMb MB)"
