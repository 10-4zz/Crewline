#!/usr/bin/env bash
set -euo pipefail

# Build a standalone Linux x86_64 Crewline release archive using PyInstaller >= 6.9 (onedir).
# Never modifies the global Python/Conda environment: all build dependencies are
# installed inside a temporary virtualenv.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
PYTHON_BIN="${PYTHON_BIN:-}"
BUILD_VENV="${BUILD_VENV:-}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
KEEP_VENV=0

usage() {
  cat <<'EOF'
用法: ./scripts/build_linux_dist.sh [选项]

选项:
  --python <path>       用于创建临时构建 venv 的 Python 3.11+ 解释器（默认使用 PATH 中的 python3）
  --venv <dir>          指定构建用临时 venv 目录（默认使用 /tmp 下全新临时目录）
  --index-url <url>     pip 镜像源（未指定时默认依次尝试系统默认源与清华镜像源）
  --keep-venv           构建完成后保留临时 venv（便于重复构建）
  -h, --help            显示此帮助

环境变量:
  PYTHON_BIN / BUILD_VENV / PIP_INDEX_URL 同上述选项。
EOF
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
  printf '%s' "${ver:-unknown}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      [[ $# -ge 2 ]] || { echo "错误: --python 需要参数" >&2; exit 2; }
      PYTHON_BIN="$2"
      shift 2
      ;;
    --venv)
      [[ $# -ge 2 ]] || { echo "错误: --venv 需要参数" >&2; exit 2; }
      BUILD_VENV="$2"
      shift 2
      ;;
    --index-url)
      [[ $# -ge 2 ]] || { echo "错误: --index-url 需要参数" >&2; exit 2; }
      PIP_INDEX_URL="$2"
      shift 2
      ;;
    --keep-venv)
      KEEP_VENV=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "错误: 未知参数 $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
if [[ "${OS_NAME}" != "Linux" || "${ARCH_NAME}" != "x86_64" ]]; then
  echo "错误: 仅支持在 Linux x86_64 主机构建（当前: ${OS_NAME} ${ARCH_NAME}）。" >&2
  exit 1
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "${PYTHON_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
  echo "错误: PATH 中未找到可用的 python3，请通过 --python 指定 Python 3.11+。" >&2
  exit 1
fi

PY_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "错误: 构建需要 Python >= 3.11（当前 ${PYTHON_BIN} 为 ${PY_VERSION}），请通过 --python 指定 Python 3.11+。" >&2
  exit 1
fi

VERSION="$("${PYTHON_BIN}" -c "
import pathlib, re
text = pathlib.Path('${ROOT_DIR}/src/crewline/__init__.py').read_text(encoding='utf-8')
m = re.search(r'__version__\s*=\s*\"([^\"]+)\"', text)
assert m, 'Cannot parse __version__'
print(m.group(1))
")"

GLIBC_VERSION="$(detect_glibc_version)"

WORK_TMP="$(mktemp -d "${TMPDIR:-/tmp}/crewline-pyi-work.XXXXXX")"
AUTO_VENV=0
if [[ -z "${BUILD_VENV}" ]]; then
  BUILD_VENV="$(mktemp -d "${TMPDIR:-/tmp}/crewline-build-venv.XXXXXX")"
  AUTO_VENV=1
fi

cleanup() {
  rm -rf "${WORK_TMP}"
  if [[ "${AUTO_VENV}" -eq 1 && "${KEEP_VENV}" -eq 0 ]]; then
    rm -rf "${BUILD_VENV}"
  fi
}
trap cleanup EXIT

echo "[1/5] 准备临时构建 venv: ${BUILD_VENV} (Python ${PY_VERSION}, glibc ${GLIBC_VERSION})"
if [[ ! -x "${BUILD_VENV}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${BUILD_VENV}"
fi

VENV_PY="${BUILD_VENV}/bin/python"
VENV_PYI="${BUILD_VENV}/bin/pyinstaller"

install_build_deps() {
  local idx="$1"
  shift
  if [[ -n "${idx}" ]]; then
    PIP_CONFIG_FILE=/dev/null "${VENV_PY}" -m pip install --no-cache-dir --index-url "${idx}" "$@"
  else
    "${VENV_PY}" -m pip install --no-cache-dir "$@"
  fi
}

echo "[2/5] 在临时 venv 内安装构建依赖（click>=8.1 / pyinstaller>=6.9）"
NEED_DEPS=()
if ! "${VENV_PY}" -c "import click" >/dev/null 2>&1; then
  NEED_DEPS+=("click>=8.1")
fi
if ! "${VENV_PY}" -c "import PyInstaller, packaging.version as v; raise SystemExit(0 if v.Version(PyInstaller.__version__) >= v.Version('6.9') else 1)" >/dev/null 2>&1; then
  NEED_DEPS+=("pyinstaller>=6.9")
fi

if [[ ${#NEED_DEPS[@]} -gt 0 ]]; then
  if [[ -n "${PIP_INDEX_URL}" ]]; then
    install_build_deps "${PIP_INDEX_URL}" "${NEED_DEPS[@]}"
  else
    if ! install_build_deps "" "${NEED_DEPS[@]}"; then
      echo "提示: 默认 pip 源安装失败，尝试使用清华 PyPI 镜像源..." >&2
      install_build_deps "https://pypi.tuna.tsinghua.edu.cn/simple" "${NEED_DEPS[@]}"
    fi
  fi
fi

PYI_VERSION="$("${VENV_PYI}" --version)"
BUNDLE_NAME="crewline-${VERSION}-linux-x86_64"
ARCHIVE_NAME="${BUNDLE_NAME}.tar.gz"
ARCHIVE_PATH="${DIST_DIR}/${ARCHIVE_NAME}"
SHA_PATH="${ARCHIVE_PATH}.sha256"

echo "[3/5] 使用 PyInstaller ${PYI_VERSION} (--onedir) 打包 Crewline ${VERSION}"
PYI_DIST="${WORK_TMP}/dist"
"${VENV_PYI}" \
  --noconfirm \
  --clean \
  --onedir \
  --name crewline \
  --paths "${ROOT_DIR}/src" \
  --distpath "${PYI_DIST}" \
  --workpath "${WORK_TMP}/build" \
  --specpath "${WORK_TMP}/spec" \
  --collect-submodules crewline \
  "${ROOT_DIR}/packaging/pyinstaller_entry.py"

STAGED_BUNDLE="${WORK_TMP}/${BUNDLE_NAME}"
mv "${PYI_DIST}/crewline" "${STAGED_BUNDLE}"

cat > "${STAGED_BUNDLE}/BUILD_INFO.txt" <<EOF
package=crewline
version=${VERSION}
os=linux
arch=x86_64
bundle_mode=onedir
packager=PyInstaller ${PYI_VERSION}
python=${PY_VERSION}
build_glibc=${GLIBC_VERSION}
built_at_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
runtime_deps=git,tmux
EOF

echo "[4/5] 校验打包产物可执行性"
"${STAGED_BUNDLE}/crewline" --version

echo "[5/5] 生成压缩包与 SHA-256 校验文件"
mkdir -p "${DIST_DIR}"
rm -f "${ARCHIVE_PATH}" "${SHA_PATH}"
tar -C "${WORK_TMP}" -czf "${ARCHIVE_PATH}" "${BUNDLE_NAME}"
(
  cd "${DIST_DIR}"
  sha256sum "${ARCHIVE_NAME}" > "${SHA_PATH}"
)

echo ""
echo "构建完成："
echo "  发行包:   ${ARCHIVE_PATH}"
echo "  校验文件: ${SHA_PATH}"
cat "${SHA_PATH}"
