# Shared Windows bootstrap for install.ps1 / uninstall.ps1.
#
# 与 scripts/entrypoint.sh 同职责：解析一个满足 CodeKit 下限的系统 Python。下限的声明在
# scripts/installer_config.py 的 MINIMUM_PYTHON，两处必须一致。
#
# Windows 专有的两点：
#   * 优先 `py -3`，回退 `python`；`python3` 在干净 Windows 上是微软商店别名桩——能找到
#     但执行后什么都不输出；
#   * 一律用 -CommandType Application 解析，避免命中 PowerShell 把 curl/wget 之类包成
#     别名时的同名假命令。
#
# 被 dot-source，不直接执行。

# 返回 @{ Exe = <路径>; Args = @(...) }；找不到可用的解释器时返回 $null。
function Resolve-CodeKitPython {
    $probe = 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)'
    foreach ($candidate in @(
        @{ Name = 'py'; Args = @('-3') },
        @{ Name = 'python'; Args = @() }
    )) {
        $resolved = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $resolved) { continue }
        $ok = $false
        try {
            & $resolved.Source @($candidate.Args + @('-c', $probe)) 2>$null
            $ok = ($LASTEXITCODE -eq 0)
        } catch { $ok = $false }
        if ($ok) { return @{ Exe = $resolved.Source; Args = $candidate.Args } }
    }
    Write-Host '  FAIL 未找到满足要求的 Python：CodeKit 需要 Python >= 3.10 才能运行。' -ForegroundColor Red
    Write-Host '       安装：winget install Python.Python.3.13 | https://www.python.org/downloads/windows/'
    return $null
}
