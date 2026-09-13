# CodeKit Plugin uninstall bootstrap for Windows.
#
# Independent entry point: it prepares no install dependencies and hands the
# work to scripts/uninstaller.py. Keep this file UTF-8 with BOM, otherwise
# PowerShell 5.1 reads the Chinese messages as ANSI.
#
# The Python launcher is resolved with -CommandType Application, and prefers
# `py -3` over a bare `python`; `python3` is deliberately never used because on
# a clean Windows that name is a Microsoft Store alias stub.

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $env:LOCALAPPDATA 'CodeKit'
$LogFile = Join-Path $StateDir 'install.log'
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
if (-not $Arguments) { $Arguments = @() }

function Write-Log {
    param([string] $Level, [string] $Message)
    try { Add-Content -Path $LogFile -Value "[$Level] $Message" -Encoding utf8 } catch { }
}
function Write-ErrLine {
    param([string] $Message)
    Write-Host "  FAIL $Message" -ForegroundColor Red
    Write-Log 'ERROR' $Message
}

function Show-Usage {
    @(
        'Usage: uninstall.cmd [-y|--yes]',
        '',
        '  -y, --yes   skip confirmation and use default selections',
        '  -h, --help  show this help'
    ) | ForEach-Object { Write-Host $_ }
}

foreach ($argument in $Arguments) {
    switch -Regex ($argument) {
        '^(-h|--help)$' { Show-Usage; exit 0 }
        '^(-y|--yes)$' { }
        default {
            Write-ErrLine "Unknown option: $argument"
            Show-Usage
            exit 2
        }
    }
}

# 卸载不自动安装 Python；缺少时明确提示用户手动补齐后重试。
$PythonExe = $null
$PythonArgs = @()
$launcher = Get-Command 'py' -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($launcher) {
    $PythonExe = $launcher.Source
    $PythonArgs = @('-3')
} else {
    $fallback = Get-Command 'python' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($fallback) { $PythonExe = $fallback.Source }
}
if (-not $PythonExe) {
    Write-ErrLine 'Python3 is required to uninstall CodeKit Plugin. Install it via: winget install Python.Python.3.13'
    exit 1
}

$uninstaller = Join-Path $ScriptDir 'scripts\uninstaller.py'
& $PythonExe @PythonArgs -X utf8 $uninstaller @Arguments
exit $LASTEXITCODE
