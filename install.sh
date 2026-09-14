#!/usr/bin/env bash
# CodeKit Plugin installer entry point (macOS / Linux).
#
# 这个脚本只负责跨过解释器这道门槛：找到一个系统 Python，转发给
# scripts/installer.py。系统依赖预检、组件选择、安装计划全在 Python 里，与 Windows
# 入口共用同一份实现——不要在这里补检查，第二份预检正是搬走它的原因。
#
# `install.sh uninstall` 仍然转交 uninstall.sh，保持旧命令可用。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P)"
OS_NAME="$(uname -s 2>/dev/null || printf 'Unknown')"

if [ "${1:-}" = "uninstall" ]; then
  shift
  exec "${SCRIPT_DIR}/uninstall.sh" "$@"
fi

# 只支持 macOS/Linux；Windows 走 install.ps1 / install.cmd，不经过 MSYS/MINGW 的 POSIX 入口。
case "${OS_NAME}" in
  Darwin|Linux) ;;
  MINGW*|MSYS*|CYGWIN*)
    printf '  ✗  Windows 请使用 install.cmd（或 install.ps1），不要用 Git Bash 运行 install.sh。\n' >&2
    exit 1
    ;;
  *)
    printf '  ✗  Unsupported OS: %s. Only macOS/Linux/Windows.\n' "${OS_NAME}" >&2
    exit 1
    ;;
esac

ENTRYPOINT="${SCRIPT_DIR}/scripts/entrypoint.sh"
if [ ! -f "${ENTRYPOINT}" ]; then
  printf '  ✗  残缺检出：缺少 %s\n' "${ENTRYPOINT}" >&2
  exit 1
fi
# shellcheck source=scripts/entrypoint.sh
. "${ENTRYPOINT}"

CODEKIT_PYTHON="$(codekit_python)" || exit 1
exec "${CODEKIT_PYTHON}" "${SCRIPT_DIR}/scripts/installer.py" "$@"
