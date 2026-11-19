#!/usr/bin/env bash
# 安装本项目构建和隔离网络测试所需的工具；不执行系统整体升级。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p build/logs
export DEBIAN_FRONTEND=noninteractive
apt_opts=(-o Acquire::Retries=1 -o Acquire::http::Timeout=20)
if [[ -n "${KND_APT_MIRROR:-}" ]]; then
  # 只生成本次命令使用的 deb822 源文件，沿用发行版 Signed-By。
  mkdir -p .cache
  python3 - <<'PY'
import os
from pathlib import Path
mirror = os.environ['KND_APT_MIRROR'].rstrip('/')
if not mirror.startswith('https://'):
    raise SystemExit('mirror must use HTTPS')
source = Path('/etc/apt/sources.list.d/ubuntu.sources').read_text()
for original in ('http://archive.ubuntu.com/ubuntu', 'http://security.ubuntu.com/ubuntu'):
    source = source.replace(original, mirror)
Path('.cache/ubuntu-mirror.sources').write_text(source)
PY
  apt_opts+=(-o "Dir::Etc::sourcelist=$PWD/.cache/ubuntu-mirror.sources" -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0)
fi
apt-get "${apt_opts[@]}" update >build/logs/apt-update.log 2>&1
apt-get -y --no-install-recommends "${apt_opts[@]}" install \
  clang llvm libbpf-dev libelf-dev bpftool golang-go make gcc pkg-config \
  iptables tcpdump conntrack iperf3 ethtool ca-certificates curl git openssh-client \
  qemu-system-x86 qemu-utils cloud-image-utils \
  >build/logs/apt-install.log 2>&1
{
  uname -a
  clang --version
  go version
  bpftool version
  ip -V
  tc -V
  qemu-system-x86_64 --version
  dpkg-query -W clang llvm libbpf-dev bpftool golang-go iptables conntrack qemu-system-x86
} >build/logs/toolchain.txt
cat build/logs/toolchain.txt
