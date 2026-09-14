# CodeKit Plugin installer entry point for Windows.
#
# 只负责跨过解释器这道门槛：解析一个满足下限的系统 Python，加上 `-X utf8`，转发给
# scripts/installer.py。系统依赖预检、组件选择、安装计划全在 Python 里，与
# install.sh 共用同一份实现——不要在这里补检查，第二份预检正是搬走它的原因。
#
# `-X utf8` 不是可选项：默认 stdout 编码是 ANSI 代码页，装不了安装器打印的框线字符。
# `install.ps1 uninstall` 仍然转交 uninstall.ps1，保持旧命令可用。

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Arguments) { $Arguments = @() }

if ($Arguments.Count -gt 0 -and $Arguments[0] -eq 'uninstall') {
    $rest = @()
    if ($Arguments.Count -gt 1) { $rest = $Arguments[1..($Arguments.Count - 1)] }
    & (Join-Path $ScriptDir 'uninstall.ps1') @rest
    exit $LASTEXITCODE
}

$Entrypoint = Join-Path $ScriptDir 'scripts\entrypoint.ps1'
if (-not (Test-Path $Entrypoint)) {
    Write-Host "  FAIL 残缺检出：缺少 $Entrypoint" -ForegroundColor Red
    exit 1
}
. $Entrypoint

$Python = Resolve-CodeKitPython
if (-not $Python) { exit 1 }

$installer = Join-Path $ScriptDir 'scripts\installer.py'
& $Python.Exe @($Python.Args) -X utf8 $installer @Arguments
exit $LASTEXITCODE
