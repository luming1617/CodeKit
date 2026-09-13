# CodeKit Plugin installer bootstrap for Windows.
#
# Mirrors install.sh: this script only *checks* the hard dependencies, reports
# every missing or too-old one with manual install commands, and aborts before
# any component install starts. scripts/installer.py owns everything else.
#
# Windows-only details that matter here:
#   * the launcher is `py -3`, never `python3` -- on a clean Windows that name is
#     a Microsoft Store alias stub that prints nothing;
#   * `-X utf8` is mandatory, because the default stdout encoding is the ANSI
#     code page, which cannot represent the glyphs the installer prints;
#   * tools must be resolved with -CommandType Application: PowerShell 5.1 ships
#     `curl` and `wget` as aliases for Invoke-WebRequest, so calling the alias
#     would silently skip the real curl.exe the installer needs.

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
function Write-Info {
    param([string] $Message)
    Write-Host $Message
    Write-Log 'INFO ' $Message
}
function Write-Ok {
    param([string] $Message)
    Write-Host "  OK   $Message" -ForegroundColor Green
    Write-Log 'OK   ' $Message
}
function Write-ErrLine {
    param([string] $Message)
    Write-Host "  FAIL $Message" -ForegroundColor Red
    Write-Log 'ERROR' $Message
}

function Show-Usage {
    @(
        'Usage: install.cmd install [-y]',
        '       uninstall.cmd [-y]',
        '',
        '  install     install or upgrade selected components',
        '  -y, --yes   proceed with default selections',
        '',
        '  Selecting a platform''s CodeKit plugin also enables that platform''s',
        '  subagent capability where a switch exists (Codex: multi_agent_v2);',
        '  a full restart of the agent app is then required.'
    ) | ForEach-Object { Write-Host $_ }
}

# 只解析真实的可执行文件，避免命中 PowerShell 的 curl/wget 别名。
function Resolve-Executable {
    param([string] $Command)
    return Get-Command $Command -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

# 从带前缀的版本文本中提取最多四段数字版本。
function Get-VersionMatch {
    param([string] $Text)
    $match = [regex]::Match([string]$Text, '\d+(\.\d+){1,3}')
    if ($match.Success) { return $match.Value }
    return ''
}

# 按数字段逐段比较版本，缺失段补零。
function Test-AtLeast {
    param([string] $Current, [string] $Minimum)
    $currentParts = @((Get-VersionMatch $Current) -split '\.')
    $minimumParts = @((Get-VersionMatch $Minimum) -split '\.')
    if (-not $currentParts[0] -or -not $minimumParts[0]) { return $false }
    for ($index = 0; $index -lt 4; $index++) {
        $left = if ($index -lt $currentParts.Count) { [int]$currentParts[$index] } else { 0 }
        $right = if ($index -lt $minimumParts.Count) { [int]$minimumParts[$index] } else { 0 }
        if ($left -gt $right) { return $true }
        if ($left -lt $right) { return $false }
    }
    return $true
}

$script:Failures = 0

# 以醒目方式记录一个不满足的强依赖；所有缺失项会在流程末尾统一汇总。
function Add-Failure {
    param([string] $Display, [string] $Required, [string] $Current, [string] $Guidance)
    $script:Failures += 1
    Write-Host ''
    Write-Host "  FAIL $Display 不满足安装要求" -ForegroundColor Red
    Write-Host "     - 要求：$Required"
    Write-Host "     - 当前：$Current"
    Write-Host "     - 安装：$Guidance"
    Write-Log 'ERROR' "$Display required $Required, current $Current; install via: $Guidance"
}

# 检查一个必须存在的命令，可选最低版本。
function Test-Tool {
    param(
        [string] $Command,
        [string[]] $VersionArgs,
        [string] $Display,
        [string] $Minimum,
        [string] $Guidance
    )
    $resolved = Resolve-Executable $Command
    if (-not $resolved) {
        $required = if ($Minimum) { ">= $Minimum" } else { '存在即可' }
        Add-Failure $Display $required '未安装' $Guidance
        return
    }
    $text = ''
    try { $text = [string]((& $resolved.Source @VersionArgs 2>$null | Select-Object -First 1)) } catch { $text = '' }
    if (-not $text.Trim()) { $text = 'unknown' }
    if ($Minimum -and -not (Test-AtLeast $text $Minimum)) {
        Add-Failure $Display ">= $Minimum" $text $Guidance
        return
    }
    Write-Ok "$Display ready: $text"
}

# 兼容旧调用方式：转交独立卸载入口，避免执行安装依赖检查。
if ($Arguments.Count -gt 0 -and $Arguments[0] -eq 'uninstall') {
    $rest = @()
    if ($Arguments.Count -gt 1) { $rest = $Arguments[1..($Arguments.Count - 1)] }
    & (Join-Path $ScriptDir 'uninstall.ps1') @rest
    exit $LASTEXITCODE
}

# 在依赖检查前处理帮助，避免用户只查看用法时触发检查动作。
if ($Arguments -contains '-h' -or $Arguments -contains '--help') {
    Show-Usage
    exit 0
}

$NodeGuidance = 'winget install OpenJS.NodeJS.LTS | nvm-windows: https://github.com/coreybutler/nvm-windows | https://nodejs.org/en/download'
$PythonGuidance = 'winget install Python.Python.3.13 | https://www.python.org/downloads/windows/'
$GitGuidance = 'winget install Git.Git | https://git-scm.com/install/windows'
$DownloaderGuidance = 'Windows 10 1803+ 自带 curl.exe；缺失时 winget install curl.curl'

Write-Info 'Checking system dependencies (git, Node.js >= 24, Python3 >= 3.10, npm, curl)...'
Write-Info 'CodeKit does not install system dependencies; missing items are reported with manual steps.'
Write-Host ''

Test-Tool -Command 'node' -VersionArgs @('-v') -Display 'Node.js' -Minimum '24.0.0' -Guidance $NodeGuidance
Test-Tool -Command 'npm' -VersionArgs @('--version') -Display 'npm' -Guidance "与 Node.js 一同安装：$NodeGuidance"
Test-Tool -Command 'git' -VersionArgs @('--version') -Display 'git' -Guidance $GitGuidance
Test-Tool -Command 'curl' -VersionArgs @('--version') -Display 'curl' -Guidance $DownloaderGuidance

# Python 必须走 py 启动器或 python；python3 在干净 Windows 上是商店别名桩。
$PythonExe = $null
$PythonArgs = @()
$launcher = Resolve-Executable 'py'
if ($launcher) {
    $PythonExe = $launcher.Source
    $PythonArgs = @('-3')
} else {
    $fallback = Resolve-Executable 'python'
    if ($fallback) { $PythonExe = $fallback.Source }
}
if (-not $PythonExe) {
    Add-Failure 'Python3' '>= 3.10' '未安装（py 启动器与 python 均不存在）' $PythonGuidance
} else {
    $version = ''
    try { $version = [string]((& $PythonExe @PythonArgs '--version' 2>&1 | Select-Object -First 1)) } catch { $version = '' }
    if (-not $version.Trim()) {
        Add-Failure 'Python3' '>= 3.10' '无法探测版本（py -3 未安装解释器）' $PythonGuidance
    } elseif (-not (Test-AtLeast $version '3.10')) {
        Add-Failure 'Python3' '>= 3.10' $version $PythonGuidance
    } else {
        Write-Ok "Python3 ready: $version"
    }
}

if ($script:Failures -gt 0) {
    Write-Host ''
    Write-ErrLine "检测到 $($script:Failures) 项系统依赖不满足要求，安装已中止。"
    Write-Host '     请按上面的“安装”提示补齐后重新运行：install.cmd' -ForegroundColor Red
    Write-Log 'ERROR' "aborted: $($script:Failures) unmet system dependencies"
    exit 1
}

Write-Ok 'All system dependencies are ready; starting Python installer.'
$installer = Join-Path $ScriptDir 'scripts\installer.py'
& $PythonExe @PythonArgs -X utf8 $installer @Arguments
exit $LASTEXITCODE
