#!/bin/bash
# 启动 KTV（可在 Finder 里双击）。首次运行会自动安装依赖。
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  # 导出系统钥匙串里的根证书：公司代理会替换 HTTPS 证书，不导出的话 pip/yt-dlp 会报证书错误
  security find-certificate -a -p /Library/Keychains/System.keychain \
    /System/Library/Keychains/SystemRootCertificates.keychain > ca.pem
  python3 -m venv venv
  ./venv/bin/pip install -q --cert ca.pem -r requirements.txt || exit 1
fi
# macOS 终端默认最多打开 256 个文件，每个手机连接占一个，调高以免人多时连不上
ulimit -n 4096 2>/dev/null
export SSL_CERT_FILE="$PWD/ca.pem" REQUESTS_CA_BUNDLE="$PWD/ca.pem"
exec ./venv/bin/python server.py
