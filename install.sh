#!/usr/bin/env bash
set -euo pipefail

# Local installer for Crewline Linux x86_64 standalone release archives.
# - Installs to user directories only (no sudo, no runtime pip/Python needed).
# - Verifies OS/arch, glibc compatibility, Git, tmux, and SHA-256 checksum BEFORE touching any install.
# - Stages and smoke-tests the new binary before a single atomic entrypoint symlink flip, so any
#   failure leaves an existing working installation completely intact.

DEFAULT_MIN_GLIBC="2.31"
TARBALL=""
CHECKSUM_FILE=""
PREFIX_DIR=""
BIN_DIR=""
DATA_DIR=""

usage() {
  cat <<'EOF'
用法: ./install.sh <本地发行包.tar.gz> [选项]

选项:
  --checksum <file>   指定 SHA-256 校验文件（默认读取 <发行包>.sha256）
  --prefix <dir>      安装前缀目录（设置 bin 为 <dir>/bin，数据目录为 <dir>/share/crewline）
  --bin-dir <dir>     启动入口目录（默认: ${XDG_BIN_HOME:-$HOME/.local/bin}）
  --data-dir <dir>    运行时数据目录（默认: ${XDG_DATA_HOME:-$HOME/.local/share}/crewline）
  -h, --help          显示此帮助

示例:
  ./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz
  ./install.sh dist/crewline-0.1.0-linux-x86_64.tar.gz --prefix /tmp/crewline-test
EOF
}

die() {
  echo "错误: $*" >&2
  exit 1
}

to_abs_path() {
  local p="$1"
  case "${p}" in
    /*) printf '%s' "${p}" ;;
    *)  printf '%s/%s' "$(pwd -P)" "${p}" ;;
  esac
}

detect_glibc_version() {
  local ver=""
  if command -v getconf >/dev/null 2>&1; then
    ver="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
  fi
  if [[ -z "${ver}" ]] && command -v ldd >/dev/null 2>&1; then
    local ldd_out=""
    ldd_out="$(ldd --version 2>&1 || true)"
    ver="$(printf '%s\n' "${ldd_out}" | awk 'NR==1 { if (match($0, /[0-9]+\.[0-9]+$/)) print substr($0, RSTART, RLENGTH) }')"
  fi
  printf '%s' "${ver}"
}

check_min_glibc() {
  local host_glibc="$1"
  local required_glibc="$2"
  if [[ -z "${host_glibc}" || ! "${required_glibc}" =~ ^[0-9]+\.[0-9]+$ ]]; then
    return 0
  fi
  local lowest=""
  lowest="$(printf '%s\n%s\n' "${required_glibc}" "${host_glibc}" | sort -V | awk 'NR==1 {print; exit}')"
  if [[ "${lowest}" != "${required_glibc}" ]]; then
    die "当前系统 glibc (${host_glibc}) 低于该发行包要求 (${required_glibc})。"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --checksum)
      [[ $# -ge 2 ]] || die "--checksum 需要参数"
      CHECKSUM_FILE="$2"
      shift 2
      ;;
    --prefix)
      [[ $# -ge 2 ]] || die "--prefix 需要参数"
      PREFIX_DIR="$2"
      shift 2
      ;;
    --bin-dir)
      [[ $# -ge 2 ]] || die "--bin-dir 需要参数"
      BIN_DIR="$2"
      shift 2
      ;;
    --data-dir)
      [[ $# -ge 2 ]] || die "--data-dir 需要参数"
      DATA_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      die "未知选项: $1"
      ;;
    *)
      if [[ -n "${TARBALL}" ]]; then
        die "只能指定一个本地发行包路径（多余参数: $1）"
      fi
      TARBALL="$1"
      shift
      ;;
  esac
done

if [[ -z "${TARBALL}" ]]; then
  usage >&2
  die "请提供本地发行包路径（.tar.gz）。本安装脚本仅从本地发行包安装，不内置外部下载地址。"
fi

# 1. Verify OS, architecture, and baseline glibc compatibility.
OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
if [[ "${OS_NAME}" != "Linux" || "${ARCH_NAME}" != "x86_64" ]]; then
  die "此发行包仅支持 Linux x86_64（当前系统: ${OS_NAME} ${ARCH_NAME}）。"
fi

HOST_GLIBC="$(detect_glibc_version)"
check_min_glibc "${HOST_GLIBC}" "${DEFAULT_MIN_GLIBC}"

# 2. Verify runtime and archive dependencies.
command -v git >/dev/null 2>&1 || die "未检测到 git，请先安装 Git（例如 sudo apt install git）。"
command -v tmux >/dev/null 2>&1 || die "未检测到 tmux，请先安装 tmux（例如 sudo apt install tmux）。"
command -v tar >/dev/null 2>&1 || die "未检测到 tar。"

SHA_CMD=""
if command -v sha256sum >/dev/null 2>&1; then
  SHA_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  SHA_CMD="shasum -a 256"
else
  die "未检测到 sha256sum 或 shasum，无法校验发行包完整性。"
fi

# 3. Verify tarball and SHA-256 checksum BEFORE touching any target directory.
TARBALL="$(to_abs_path "${TARBALL}")"
[[ -f "${TARBALL}" && -r "${TARBALL}" ]] || die "找不到可读的发行包文件: ${TARBALL}"

if [[ -z "${CHECKSUM_FILE}" ]]; then
  CHECKSUM_FILE="${TARBALL}.sha256"
else
  CHECKSUM_FILE="$(to_abs_path "${CHECKSUM_FILE}")"
fi
[[ -f "${CHECKSUM_FILE}" && -r "${CHECKSUM_FILE}" ]] || die "找不到 SHA-256 校验文件: ${CHECKSUM_FILE}（可通过 --checksum 指定）。"

EXPECTED_SHA="$(awk 'NF>=1 {print $1; exit}' "${CHECKSUM_FILE}" | tr '[:upper:]' '[:lower:]')"
if [[ ! "${EXPECTED_SHA}" =~ ^[0-9a-f]{64}$ ]]; then
  die "校验文件格式无效: ${CHECKSUM_FILE}"
fi

ACTUAL_SHA="$(${SHA_CMD} "${TARBALL}" | awk '{print $1}' | tr '[:upper:]' '[:lower:]')"
if [[ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]]; then
  die "SHA-256 校验失败！期望 ${EXPECTED_SHA}，实际 ${ACTUAL_SHA}。已中止安装，未改动任何现有安装。"
fi

# 4. Resolve and normalize install directories to absolute paths (user-writable, no sudo).
if [[ -n "${PREFIX_DIR}" ]]; then
  ABS_PREFIX="$(to_abs_path "${PREFIX_DIR}")"
  BIN_DIR="$(to_abs_path "${BIN_DIR:-${ABS_PREFIX}/bin}")"
  DATA_DIR="$(to_abs_path "${DATA_DIR:-${ABS_PREFIX}/share/crewline}")"
else
  BIN_DIR="$(to_abs_path "${BIN_DIR:-${XDG_BIN_HOME:-${HOME}/.local/bin}}")"
  DATA_DIR="$(to_abs_path "${DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/crewline}")"
fi

# 5. Stage extraction in a temporary directory and smoke-test BEFORE mutating the live entrypoint.
mkdir -p "${DATA_DIR}/releases" "${BIN_DIR}"
BIN_DIR="$(cd "${BIN_DIR}" && pwd -P)"
DATA_DIR="$(cd "${DATA_DIR}" && pwd -P)"

STAGE_DIR="$(mktemp -d "${DATA_DIR}/.staging.XXXXXX")"
RELEASE_DIR=""
TMP_BIN_LINK=""
ACTIVATED=0

cleanup_on_exit() {
  rm -rf "${STAGE_DIR}"
  if [[ -n "${TMP_BIN_LINK}" ]]; then
    rm -f "${TMP_BIN_LINK}"
  fi
  if [[ "${ACTIVATED}" -eq 0 && -n "${RELEASE_DIR}" && -d "${RELEASE_DIR}" ]]; then
    rm -rf "${RELEASE_DIR}"
  fi
}
trap cleanup_on_exit EXIT

tar -xzf "${TARBALL}" -C "${STAGE_DIR}"

STAGED_BUNDLE=""
for candidate in "${STAGE_DIR}"/*; do
  if [[ -d "${candidate}" && -x "${candidate}/crewline" ]]; then
    STAGED_BUNDLE="${candidate}"
    break
  fi
done

if [[ -z "${STAGED_BUNDLE}" ]]; then
  die "发行包结构无效：未找到可执行的 crewline 目录。"
fi

# If BUILD_INFO.txt records build_glibc, enforce host glibc >= build_glibc.
if [[ -f "${STAGED_BUNDLE}/BUILD_INFO.txt" ]]; then
  PKG_GLIBC="$(awk -F= '$1=="build_glibc" {print $2; exit}' "${STAGED_BUNDLE}/BUILD_INFO.txt" | tr -d '\r\n')"
  if [[ -n "${PKG_GLIBC}" && "${PKG_GLIBC}" != "unknown" ]]; then
    check_min_glibc "${HOST_GLIBC}" "${PKG_GLIBC}"
  fi
fi

# Smoke-test staged binary before installing or switching the live entrypoint.
STAGED_VERSION="$("${STAGED_BUNDLE}/crewline" --version 2>&1)" || die "发行包内 crewline 自检失败: ${STAGED_VERSION}"

BUNDLE_BASE="$(basename "${STAGED_BUNDLE}")"
SHORT_SHA="${ACTUAL_SHA:0:12}"
RELEASE_DIR="${DATA_DIR}/releases/${BUNDLE_BASE}-${SHORT_SHA}-$$"
mv "${STAGED_BUNDLE}" "${RELEASE_DIR}"

# 6. Single atomic flip: point $BIN_DIR/crewline directly to the validated absolute release binary.
# If creating or replacing the entrypoint fails, cleanup_on_exit removes the new release dir
# and leaves any existing working $BIN_DIR/crewline completely untouched.
TMP_BIN_LINK="${BIN_DIR}/.crewline.tmp.$$"
ln -sfn "${RELEASE_DIR}/crewline" "${TMP_BIN_LINK}"
mv -Tf "${TMP_BIN_LINK}" "${BIN_DIR}/crewline"
TMP_BIN_LINK=""
ACTIVATED=1

INSTALLED_VERSION="$("${BIN_DIR}/crewline" --version)"

echo "Crewline 安装成功："
echo "  版本:     ${INSTALLED_VERSION}"
echo "  启动入口: ${BIN_DIR}/crewline"
echo "  运行目录: ${RELEASE_DIR}"
echo "  SHA-256:  ${ACTUAL_SHA}"

case ":${PATH}:" in
  *":${BIN_DIR}:"*)
    ;;
  *)
    echo ""
    echo "提示: ${BIN_DIR} 当前不在 PATH 中，可执行以下命令生效："
    echo "  export PATH=\"${BIN_DIR}:\$PATH\""
    ;;
esac
