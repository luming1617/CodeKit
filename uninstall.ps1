# CodeKit Plugin uninstall entry point for Windows.
#
# 与 install.ps1 同形：解析一个满足下限的系统 Python，转发给 scripts/uninstaller.py。
# 卸载不做依赖预检——它只删东西，不需要 Node.js 或包管理器。

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Arguments) { $Arguments = @() }

$Entrypoint = Join-Path $ScriptDir 'scripts\entrypoint.ps1'
if (-not (Test-Path $Entrypoint)) {
    Write-Host "  FAIL 残缺检出：缺少 $Entrypoint" -ForegroundColor Red
    exit 1
}
. $Entrypoint

$Python = Resolve-CodeKitPython
if (-not $Python) { exit 1 }

$uninstaller = Join-Path $ScriptDir 'scripts\uninstaller.py'
& $Python.Exe @($Python.Args) -X utf8 $uninstaller @Arguments
exit $LASTEXITCODE
