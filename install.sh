#!/usr/bin/env bash
# CodeKit Plugin installer bootstrap entry point.
# System dependencies are deliberately prepared here, before Python starts.

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
# 输出可恢复问题的警告，并继续由调用方决定是否中止。
warn() { printf '  ⚠  %s\n' "$*"; _log "[WARN]  $*"; }
# 输出错误信息到 stderr，同时持久化到安装日志。
error() { printf '  ✗  %s\n' "$*" >&2; _log "[ERROR] $*"; }
# 输出成功状态，并持久化到安装日志。
success() { printf '  ✓  %s\n' "$*"; _log "[OK]    $*"; }

# 执行命令并把完整输出追加到日志；终端只显示命令级摘要，避免 apt/brew 刷屏。
_cmd_tee() {
  local command_text rc
  printf -v command_text '%q ' "$@"
  printf '  · 执行：%s\n' "${command_text% }"
  _log "[CMD]   ${command_text% }"
  "$@" >>"${LOG_FILE}" 2>&1
  rc=$?
  if [ "${rc}" -eq 0 ]; then
    printf '  ✓  完成：%s\n' "${command_text% }"
  else
    printf '  ✗  失败（退出码 %s）：%s\n' "${rc}" "${command_text% }" >&2
  fi
  return "${rc}"
}

# 在常见安装位置寻找 brew，兼容 Apple Silicon、Intel macOS 和 Linuxbrew。
_brew_bin() {
  local candidate
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
    [ -x "${candidate}" ] && { printf '%s' "${candidate}"; return 0; }
  done
  return 1
}

# 将 brew 和其 sbin 目录加入当前进程 PATH，使刚安装的依赖立即可调用。
_add_brew_path() {
  local brew_bin brew_root
  brew_bin=$(_brew_bin) || return 0
  brew_root=$(dirname "$(dirname "${brew_bin}")")
  PATH="$(dirname "${brew_bin}"):${brew_root}/sbin:${PATH}"
  export PATH
}

# Linux 使用发行版原生包管理器；Homebrew 只作为 macOS 的系统依赖来源。
LINUX_PACKAGE_MANAGER=""
LINUX_PACKAGE_INDEX_UPDATED=0

_detect_linux_package_manager() {
  local candidate
  for candidate in apt-get dnf yum pacman zypper apk; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      LINUX_PACKAGE_MANAGER="${candidate}"
      export LINUX_PACKAGE_MANAGER
      return 0
    fi
  done
  return 1
}

# 以 root 或 sudo 执行发行版包管理器命令，不在 Linux 上静默依赖 Homebrew。
_linux_privileged() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    error "Installing Linux packages requires root privileges or sudo."
    return 1
  fi
}

_linux_package_for() {
  local package="$1"
  case "${LINUX_PACKAGE_MANAGER}:${package}" in
    pacman:python3|pacman:python3-venv) printf '%s' python ;;
    pacman:node|pacman:nodejs) printf '%s' nodejs ;;
    pacman:npm) printf '%s' npm ;;
    dnf:python3-venv|yum:python3-venv|zypper:python3-venv) printf '%s' python3 ;;
    apk:python3-venv) printf '%s' py3-virtualenv ;;
    *:node) printf '%s' nodejs ;;
    *) printf '%s' "${package}" ;;
  esac
}

_linux_package_update() {
  [ "${LINUX_PACKAGE_INDEX_UPDATED}" -eq 1 ] && return 0
  case "${LINUX_PACKAGE_MANAGER}" in
    apt-get) _linux_privileged apt-get update || return 1 ;;
    zypper) _linux_privileged zypper --non-interactive refresh || return 1 ;;
  esac
  LINUX_PACKAGE_INDEX_UPDATED=1
}

_linux_package_install() {
  [ -n "${LINUX_PACKAGE_MANAGER}" ] || _detect_linux_package_manager || {
    error "No supported Linux package manager found (apt-get, dnf, yum, pacman, zypper, or apk)."
    return 1
  }
  _linux_package_update || return 1
  case "${LINUX_PACKAGE_MANAGER}" in
    apt-get) _linux_privileged apt-get install -y "$@" ;;
    dnf) _linux_privileged dnf install -y "$@" ;;
    yum) _linux_privileged yum install -y "$@" ;;
    pacman) _linux_privileged pacman -S --needed --noconfirm "$@" ;;
    zypper) _linux_privileged zypper --non-interactive install "$@" ;;
    apk) _linux_privileged apk add --no-cache "$@" ;;
    *) error "Unsupported Linux package manager: ${LINUX_PACKAGE_MANAGER}"; return 1 ;;
  esac
}

# 注入中科大镜像地址，供 brew update、formula 和 bottle 下载使用。
_setup_brew_mirrors() {
  export HOMEBREW_BREW_GIT_REMOTE="https://mirrors.ustc.edu.cn/brew.git"
  export HOMEBREW_API_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles/api"
  export HOMEBREW_ARTIFACT_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles"
}

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

# 下载远程脚本文本，优先使用 curl，缺失时回退 wget。
_fetch_installer_script() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "${url}"
  else
    return 1
  fi
}

# 通过镜像安装 Homebrew，并在安装后重新刷新 PATH 和 brew 可用性。
_install_homebrew() {
  local url="https://mirrors.ustc.edu.cn/misc/brew-install.sh" script rc
  warn "Homebrew not found. Installing automatically..."
  if command -v curl >/dev/null 2>&1; then
    NONINTERACTIVE=1 /bin/bash -c "\$(curl -fsSL \"${url}\")" 2>&1 | tee -a "${LOG_FILE}"
    rc="${PIPESTATUS[0]}"
  else
    script=$(_fetch_installer_script "${url}") || {
      error "Homebrew installation needs curl or wget. Install one manually and retry."
      return 1
    }
    printf '%s' "${script}" | NONINTERACTIVE=1 /bin/bash 2>&1 | tee -a "${LOG_FILE}"
    rc="${PIPESTATUS[1]}"
  fi
  [ "${rc}" -eq 0 ] || return "${rc}"
  _add_brew_path
  command -v brew >/dev/null 2>&1
}

ensure_homebrew() {
  # Homebrew 是后续所有 formula 安装的基础；已有 brew 时更新失败只告警继续。
  info "Checking Homebrew..."
  _setup_brew_mirrors
  _add_brew_path
  if ! command -v brew >/dev/null 2>&1; then
    _install_homebrew || { error "Homebrew installation failed."; return 1; }
  else
    _cmd_tee brew update || warn "brew update failed; continuing without updating."
  fi
  _add_brew_path
  command -v brew >/dev/null 2>&1 || { error "brew was not found after preparation."; return 1; }
  success "Homebrew ready: $(brew --version 2>/dev/null | head -1)"
}

ensure_python3() {
  # Python 由后续 Python 编排器使用；macOS 走 brew，Linux 走发行版包管理器。
  info "Checking Python3..."
  if ! command -v python3 >/dev/null 2>&1; then
    if [ "${OS_NAME}" = "Darwin" ]; then
      _cmd_tee brew install python3 || return 1
      _add_brew_path
    else
      _linux_package_install "$(_linux_package_for python3)" "$(_linux_package_for python3-venv)" || return 1
    fi
  elif [ "${OS_NAME}" = "Linux" ] && ! python3 -m venv --help >/dev/null 2>&1; then
    _linux_package_install "$(_linux_package_for python3-venv)" || return 1
  fi
  command -v python3 >/dev/null 2>&1 || { error "Python3 installation failed."; return 1; }
  success "Python3 ready: $(command -v python3)"
}

ensure_npm() {
  # Linux 的 nodejs 包不一定默认带 npm，单独确认以保证后续 agent/工具安装可用。
  info "Checking npm..."
  if ! command -v npm >/dev/null 2>&1; then
    if [ "${OS_NAME}" = "Darwin" ]; then
      _cmd_tee brew install node || return 1
      _add_brew_path
    else
      _linux_package_install "$(_linux_package_for npm)" || return 1
    fi
  fi
  command -v npm >/dev/null 2>&1 || { error "npm installation failed."; return 1; }
  success "npm ready: $(command -v npm)"
}

ensure_curl() {
  # curl 同时用于下载远程脚本和 git-ai；macOS 使用 brew，Linux 使用系统包。
  info "Checking curl..."
  if ! command -v curl >/dev/null 2>&1; then
    if [ "${OS_NAME}" = "Darwin" ]; then
      _cmd_tee brew install curl || true
      _add_brew_path
      local brew_bin curl_bin
      brew_bin=$(_brew_bin 2>/dev/null || true)
      if [ -n "${brew_bin}" ]; then
        curl_bin="$(dirname "$(dirname "${brew_bin}")")/opt/curl/bin/curl"
        [ -x "${curl_bin}" ] && PATH="$(dirname "${curl_bin}"):${PATH}" && export PATH
      fi
    else
      _linux_package_install "$(_linux_package_for curl)" || return 1
    fi
  fi
  command -v curl >/dev/null 2>&1 || { error "curl installation failed."; return 1; }
  success "curl ready: $(curl --version 2>/dev/null | head -1)"
}

ensure_versioned() {
  # 对 git、Node.js 执行统一的版本门槛检查；升级后仍不达标则终止整个流程。
  local label="$1" command_name="$2" version_args="$3" minimum="$4" formula="$5" current
  info "Checking ${label} (>= ${minimum})..."
  current="$("${command_name}" ${version_args} 2>/dev/null | head -1)"
  if [ -n "${current}" ] && _version_ge "${current}" "${minimum}"; then
    success "${label} ready: ${current}"
    return 0
  fi
  if [ "${OS_NAME}" = "Darwin" ]; then
    [ -n "${current}" ] && warn "${label} too old (${current}); upgrading via Homebrew..." || info "${label} not found. Installing via Homebrew..."
  else
    [ -n "${current}" ] && warn "${label} too old (${current}); upgrading via Linux package manager..." || info "${label} not found. Installing via Linux package manager..."
  fi
  if [ "${OS_NAME}" = "Darwin" ]; then
    _cmd_tee brew upgrade "${formula}" || _cmd_tee brew install "${formula}" || true
    _add_brew_path
  else
    _linux_package_install "$(_linux_package_for "${formula}")" || return 1
  fi
  current="$("${command_name}" ${version_args} 2>/dev/null | head -1)"
  if [ -z "${current}" ] || ! _version_ge "${current}" "${minimum}"; then
    error "${label} >= ${minimum} is not ready."
    return 1
  fi
  success "${label} ready: ${current}"
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

# 在依赖检查前处理帮助，避免用户只查看用法时触发安装动作。
case " ${*:-} " in
  *" -h "*|*" --help "*) usage; exit 0 ;;
esac

# bootstrap 只支持 macOS/Linux；其余平台直接返回，避免执行不兼容命令。
case "${OS_NAME}" in
  Darwin|Linux) ;;
  *) error "Unsupported OS: ${OS_NAME}"; exit 1 ;;
esac

# 依赖按平台分支准备：macOS 使用 Homebrew，Linux 使用发行版原生包管理器。
if [ "${OS_NAME}" = "Darwin" ]; then
  ensure_homebrew || exit 1
else
  _detect_linux_package_manager || {
    error "No supported Linux package manager found (apt-get, dnf, yum, pacman, zypper, or apk)."
    exit 1
  }
  info "Linux package manager ready: ${LINUX_PACKAGE_MANAGER}"
fi
ensure_python3 || exit 1
ensure_npm || exit 1
ensure_curl || exit 1
ensure_versioned "git" git "--version" "2.50.1" git || exit 1
ensure_versioned "Node.js" node "-v" "20.0.0" node || exit 1

success "All system dependencies are ready; starting Python installer."
exec python3 "${SCRIPT_DIR}/scripts/installer.py" "$@"
