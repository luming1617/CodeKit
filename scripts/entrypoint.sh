#!/usr/bin/env bash
# Shared POSIX bootstrap for install.sh / uninstall.sh.
#
# 只负责一件事：找到一个满足 CodeKit 下限的系统 Python。这是唯一无法委托给被启动程序的
# 前置检查（用太旧的解释器去跑安装器，连错误提示都打印不出来），因此每次 install /
# uninstall 都要重新证明一次。下限的声明在 scripts/installer_config.py 的
# MINIMUM_PYTHON，两处必须一致。
#
# 被 source，不直接执行。系统 Python 只是用来运行安装器本身：首次安装由它把内置运行时
# 写进工具链根，之后组件侧就与它无关（graphify 的 venv 由内置运行时创建）。不要改成用
# 工具链根里的内置运行时来跑安装器——运行时正是安装器要修复的对象，自引用会让它一旦
# 损坏就再也修不回来。

# 打印可用的解释器路径；找不到或版本不足时打印指引并返回 1。
codekit_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    printf '  ✗  未找到 python3：CodeKit 需要 Python >= 3.10 才能运行。\n' >&2
    printf '     安装：https://www.python.org/downloads/\n' >&2
    return 1
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
    # 版本读不出来时不能报成空串：Windows 上的商店别名桩 `python3` 就是能 stat、执行后
    # 什么都不输出，把它说成「版本过低」会把人引到错误的排查方向。
    local reported
    reported="$(python3 --version 2>&1 | head -1)"
    printf '  ✗  python3 不可用或版本过低：%s（CodeKit 需要 >= 3.10）\n' "${reported:-无法获取版本}" >&2
    printf '     安装：https://www.python.org/downloads/\n' >&2
    return 1
  fi
  printf '%s' python3
}
