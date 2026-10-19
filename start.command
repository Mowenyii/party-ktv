#!/bin/bash
# 双击启动 KTV；自动准备 Python 环境、证书和依赖。

cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

pause_on_error() {
  local code="${1:-1}"
  if [ -t 0 ]; then
    read -r -p "按回车键关闭窗口…" _
  fi
  exit "$code"
}

if [ ! -f server.py ] || [ ! -f requirements.txt ]; then
  echo "缺少 server.py 或 requirements.txt。"
  echo "请先完整解压下载的 ZIP，再运行 start.command。"
  pause_on_error 1
fi

# Finder 启动时 PATH 可能不包含 Homebrew；依次尝试常见位置，并要求 Python 3.10+。
PYTHON_BIN=""
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  resolved="$(command -v "$candidate" 2>/dev/null || true)"
  if [ -n "$resolved" ] && "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PYTHON_BIN="$resolved"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "没有找到 Python 3.10 或更高版本。"
  echo "即将打开官方下载页；安装完成后，请重新双击 start.command。"
  open "https://www.python.org/downloads/macos/" >/dev/null 2>&1 || true
  pause_on_error 1
fi

# 导出 macOS 系统根证书，兼容使用 HTTPS 代理的公司网络；失败时保留上次可用证书。
CERT_FILE="$PWD/ca.pem"
CERT_TMP="$PWD/ca.pem.tmp"
if command -v security >/dev/null 2>&1; then
  if security find-certificate -a -p /Library/Keychains/System.keychain \
      /System/Library/Keychains/SystemRootCertificates.keychain > "$CERT_TMP" 2>/dev/null \
      && [ -s "$CERT_TMP" ]; then
    mv -f "$CERT_TMP" "$CERT_FILE"
  else
    rm -f "$CERT_TMP"
  fi
fi

PIP_CERT=()
if [ -s "$CERT_FILE" ]; then
  export SSL_CERT_FILE="$CERT_FILE"
  export REQUESTS_CA_BUNDLE="$CERT_FILE"
  PIP_CERT=(--cert "$CERT_FILE")
fi

VENV_PY="$PWD/venv/bin/python"
if [ ! -x "$VENV_PY" ] || ! "$VENV_PY" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
  echo "正在创建 Python 运行环境…"
  "$PYTHON_BIN" -m venv --clear venv || pause_on_error 1
fi

# 每次快速检查一次：依赖齐全时几乎立即完成，首次安装中断时则会自动修复。
echo "正在检查依赖…"
"$VENV_PY" -m pip install -q "${PIP_CERT[@]}" -r requirements.txt || {
  echo "依赖安装失败，请检查上面的网络或证书错误。"
  pause_on_error 1
}

# macOS 终端默认最多打开 256 个文件，每个手机连接占一个，提高并发上限。
ulimit -n 4096 2>/dev/null || true

echo "正在启动 KTV… 使用期间请保持这个窗口开启。"
"$VENV_PY" server.py
status=$?
if [ "$status" -ne 0 ]; then
  echo "KTV 异常停止（错误码 $status）。"
  pause_on_error "$status"
fi
exit 0
