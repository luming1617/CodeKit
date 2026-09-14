#!/usr/bin/env bash
# CodeKit Plugin uninstall entry point (macOS / Linux).
#
# 与 install.sh 同形：找到一个系统 Python，转发给 scripts/uninstaller.py。卸载不做依赖
# 预检——它只删东西，不需要 Node.js 或包管理器。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P)"
OS_NAME="$(uname -s 2>/dev/null || printf 'Unknown')"

case "${OS_NAME}" in
  Darwin|Linux) ;;
  MINGW*|MSYS*|CYGWIN*)
    printf '  ✗  Windows 请使用 uninstall.cmd（或 uninstall.ps1），不要用 Git Bash 运行 uninstall.sh。\n' >&2
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
exec "${CODEKIT_PYTHON}" "${SCRIPT_DIR}/scripts/uninstaller.py" "$@"
