#!/usr/bin/env bash
# CodeKit Plugin 独立卸载入口。
# 卸载只调用独立卸载编排器，不准备 Homebrew、Node.js 等安装依赖。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P)"
OS_NAME="$(uname -s 2>/dev/null || printf 'Unknown')"
XDG_STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"
STATE_DIR="${XDG_STATE_HOME}/CodeKit"
LOG_FILE="${STATE_DIR}/install.log"
mkdir -p "${STATE_DIR}" 2>/dev/null || true

# 记录卸载引导阶段的错误；日志不可写时仍允许 Python 卸载流程继续执行。
_log() {
  [ -d "${STATE_DIR}" ] || return 0
  printf '%s\n' "$*" 2>/dev/null >>"${LOG_FILE}" || true
}

# 输出错误信息到 stderr，并和安装流程共用同一个日志文件。
error() {
  printf '  ✗  %s\n' "$*" >&2
  _log "[ERROR] $*"
}
# 输出平台分支信息，并写入卸载日志。
info() {
  printf '%s\n' "$*"
  _log "[INFO]  $*"
}
# 输出可继续执行的卸载告警，并写入卸载日志。
warn() {
  printf '  ⚠  %s\n' "$*"
  _log "[WARN]  $*"
}

# 打印独立卸载脚本支持的参数。
usage() {
  printf '%s\n' \
    "Usage: bash uninstall.sh [-y|--yes]" \
    "" \
    "  -y, --yes   skip confirmation and use default selections" \
    "  -h, --help  show this help"
}

run_python_uninstall() {
  # 卸载不自动安装 Python；缺少 Python 时明确提示用户手动补齐后重试。
  if ! command -v python3 >/dev/null 2>&1; then
    error "Python3 is required to uninstall CodeKit Plugin."
    return 1
  fi

  # 先确认仓库内的卸载编排器和共享辅助模块完整，避免卸载过程中途缺少关键逻辑。
  local helper
  local -a required_helpers=(
    "${SCRIPT_DIR}/scripts/uninstaller.py"
    "${SCRIPT_DIR}/scripts/installer.py"
    "${SCRIPT_DIR}/scripts/installer_config.py"
    "${SCRIPT_DIR}/scripts/installer_state.py"
    "${SCRIPT_DIR}/scripts/installer_sources.py"
    "${SCRIPT_DIR}/scripts/component-selector.py"
  )
  for helper in "${required_helpers[@]}"; do
    if [ ! -f "${helper}" ]; then
      error "Uninstaller helper missing: ${helper}"
      return 1
    fi
  done

  # 进入独立卸载编排器，保留 -y/--yes 等用户参数。
  python3 "${SCRIPT_DIR}/scripts/uninstaller.py" "$@"
}

# 卸载脚本不接受 install/uninstall 子命令，避免把动作参数重复传给 Python。
for arg in "$@"; do
  case "${arg}" in
    -y|--yes) ;;
    -h|--help) usage; exit 0 ;;
    *) error "Unknown option: ${arg}"; usage >&2; exit 2 ;;
  esac
done

# macOS 的 brew 组件由 Python 卸载器直接处理；这里明确标记平台，供组件分支使用。
uninstall_macos() {
  export CODEKIT_PLATFORM="macos"
  export CODEKIT_PACKAGE_MANAGER="brew"
  run_python_uninstall "$@"
}

# Linux 组件优先由发行版包管理器处理；没有包管理器时仍可卸载用户态插件和工具。
uninstall_linux() {
  local manager
  for manager in apt-get dnf yum pacman zypper apk; do
    if command -v "${manager}" >/dev/null 2>&1; then
      export CODEKIT_PACKAGE_MANAGER="${manager}"
      break
    fi
  done
  export CODEKIT_PLATFORM="linux"
  if [ -n "${CODEKIT_PACKAGE_MANAGER:-}" ]; then
    info "Linux uninstall branch: ${CODEKIT_PACKAGE_MANAGER}"
  else
    warn "No supported Linux package manager found; continuing with user-level cleanup."
  fi
  run_python_uninstall "$@"
}

case "${OS_NAME}" in
  Darwin) uninstall_macos "$@"; exit $? ;;
  Linux) uninstall_linux "$@"; exit $? ;;
  *) error "Unsupported OS: ${OS_NAME}. Only macOS/Linux are supported."; exit 1 ;;
esac
