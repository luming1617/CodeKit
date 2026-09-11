#!/usr/bin/env bash
# CodeKit Plugin installer bootstrap entry point.
#
# The installer never installs OS-level tools. This script only *checks* the
# hard dependencies, reports every missing or too-old one with manual install
# commands, and aborts before any component install starts when something is
# missing.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P)"
OS_NAME="$(uname -s 2>/dev/null || printf 'Unknown')"
XDG_STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"
STATE_DIR="${XDG_STATE_HOME}/CodeKit"
LOG_FILE="${STATE_DIR}/install.log"
mkdir -p "${STATE_DIR}" 2>/dev/null || true

# 统一记录引导阶段的输出；日志目录不可写时不影响终端流程。
_log() { printf '%s\n' "$*" >>"${LOG_FILE}" 2>/dev/null || true; }
# 输出普通信息，并写入与 Python 安装器共用的日志文件。
info() { printf '%s\n' "$*"; _log "[INFO]  $*"; }
# 输出可恢复问题的警告。
warn() { printf '  ⚠  %s\n' "$*" >&2; _log "[WARN]  $*"; }
# 输出错误信息到 stderr，同时持久化到安装日志。
error() { printf '  ✗  %s\n' "$*" >&2; _log "[ERROR] $*"; }
# 输出成功状态，并持久化到安装日志。
success() { printf '  ✓  %s\n' "$*"; _log "[OK]    $*"; }

# 从带前缀的版本文本中提取最多四段数字版本。
_version_extract() {
  printf '%s' "$1" | grep -Eo '[0-9]+(\.[0-9]+){1,3}' | head -1
}

# 按数字段逐段比较两个版本，供依赖最低版本检查使用。
_version_ge() {
  awk -v left="$(_version_extract "$1")" -v right="$(_version_extract "$2")" '
    BEGIN {
      ln = split(left, l, "."); rn = split(right, r, ".");
      if (!ln || !rn || l[1] == "" || r[1] == "") exit 1;
      for (i = 1; i <= 4; i++) {
        lv = (i <= ln ? l[i] + 0 : 0); rv = (i <= rn ? r[i] + 0 : 0);
        if (lv > rv) exit 0;
        if (lv < rv) exit 1;
      }
      exit 0;
    }'
}

FAILURES=0

# 以醒目方式记录一个不满足的强依赖；所有缺失项会在流程末尾统一汇总。
_dependency_error() {
  local name="$1" required="$2" current="$3" guidance="$4"
  FAILURES=$((FAILURES + 1))
  printf '\n  ✗  %s 不满足安装要求\n' "${name}" >&2
  printf '     · 要求：%s\n' "${required}" >&2
  printf '     · 当前：%s\n' "${current}" >&2
  printf '     · 安装：%s\n' "${guidance}" >&2
  _log "[ERROR] ${name}: required ${required}, current ${current}; install via: ${guidance}"
}

# 检查一个必须存在（可选最低版本）的命令。
_check_command() {
  local name="$1" command_name="$2" version_args="$3" minimum="$4" guidance="$5" current
  current="$(command -v "${command_name}" >/dev/null 2>&1 && "${command_name}" ${version_args} 2>/dev/null | head -1 || true)"
  if [ -z "${current}" ]; then
    _dependency_error "${name}" "${minimum:-存在即可}" "未安装" "${guidance}"
    return
  fi
  if [ -n "${minimum}" ] && ! _version_ge "${current}" "${minimum}"; then
    _dependency_error "${name}" ">= ${minimum}" "${current}" "${guidance}"
    return
  fi
  success "${name} ready: ${current}"
}

_node_guidance() {
  if [ "${OS_NAME}" = "Darwin" ]; then
    printf 'brew install node@24 | nvm install 24 | https://nodejs.org/en/download'
  else
    printf 'nvm/fnm 安装 24 LTS | Debian/Ubuntu: https://deb.nodesource.com/setup_24.x | https://nodejs.org/en/download'
  fi
}

_python_guidance() {
  if [ "${OS_NAME}" = "Darwin" ]; then
    printf 'brew install python3 | https://www.python.org/downloads/'
  else
    printf '发行版包管理器安装 python3（Debian/Ubuntu 需同时安装 python3-venv）'
  fi
}

_git_guidance() {
  if [ "${OS_NAME}" = "Darwin" ]; then
    printf 'brew install git | xcode-select --install'
  else
    printf '发行版包管理器安装 git（Debian/Ubuntu: sudo apt-get install -y git）'
  fi
}

_downloader_guidance() {
  if [ "${OS_NAME}" = "Darwin" ]; then
    printf 'brew install curl（macOS 通常自带 curl）'
  else
    printf '发行版包管理器安装 curl 或 wget（Debian/Ubuntu: sudo apt-get install -y curl）'
  fi
}

usage() {
  # 打印 bootstrap 支持的子命令和非交互参数。
  printf '%s\n' \
    "Usage: bash install.sh install [-y]" \
    "       bash uninstall.sh [-y]" \
    "" \
    "  install     install or upgrade selected components" \
    "  -y, --yes   proceed with default selections"
}

# 兼容旧调用方式，但立即转交独立卸载入口，避免执行安装依赖准备。
if [ "${1:-}" = "uninstall" ]; then
  shift
  exec "${SCRIPT_DIR}/uninstall.sh" "$@"
fi

# 在依赖检查前处理帮助，避免用户只查看用法时触发检查动作。
case " ${*:-} " in
  *" -h "*|*" --help "*) usage; exit 0 ;;
esac

# bootstrap 只支持 macOS/Linux；Windows 走 install.ps1 / install.cmd。
case "${OS_NAME}" in
  Darwin|Linux) ;;
  MINGW*|MSYS*|CYGWIN*)
    error "Windows 请使用 install.cmd（或 install.ps1），不要用 Git Bash 运行 install.sh。"
    exit 1
    ;;
  *) error "Unsupported OS: ${OS_NAME}"; exit 1 ;;
esac

info "Checking system dependencies (git, Node.js >= 24, Python3 >= 3.10, npm, curl/wget)..."
info "CodeKit does not install system dependencies; missing items are reported with manual steps."
printf '\n'

# 强依赖只检测不安装：任何一项不满足都会在汇总后中止整个安装流程。
_check_command "Node.js" node "-v" "24.0.0" "$(_node_guidance)"
_check_command "npm" npm "--version" "" "与 Node.js 一同安装：$(_node_guidance)"
_check_command "Python3" python3 "--version" "3.10" "$(_python_guidance)"
_check_command "git" git "--version" "" "$(_git_guidance)"

if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  _dependency_error "curl/wget" "任一存在" "均未安装" "$(_downloader_guidance)"
else
  success "curl/wget ready: $(command -v curl 2>/dev/null || command -v wget)"
fi

if [ "${FAILURES}" -gt 0 ]; then
  printf '\n  ✗ 检测到 %s 项系统依赖不满足要求，安装已中止。\n' "${FAILURES}" >&2
  printf '    请按上面的“安装”提示补齐后重新运行：bash install.sh\n\n' >&2
  _log "[ERROR] aborted: ${FAILURES} unmet system dependencies"
  exit 1
fi

success "All system dependencies are ready; starting Python installer."
exec python3 "${SCRIPT_DIR}/scripts/installer.py" "$@"
