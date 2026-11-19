#!/usr/bin/env bash
# 固定本次环境检查选择的 CLI 版本；下载后校验 SHA256 或 Go checksum database。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source scripts/env.sh
mkdir -p .cache/bin .cache/downloads build/logs
kubectl_version=v1.37.2
kind_version=v0.33.0
download=.cache/downloads/kubectl-${kubectl_version}-linux-amd64
curl -fsSL --retry 2 --connect-timeout 10 --max-time 30 \
  "https://dl.k8s.io/release/$kubectl_version/bin/linux/amd64/kubectl.sha256" -o "$download.sha256"
if [[ "${KND_KUBECTL_MIRROR:-0}" == 1 ]] || ! curl -fSL --connect-timeout 10 --max-time 30 \
  "https://dl.k8s.io/release/$kubectl_version/bin/linux/amd64/kubectl" -o "$download.tmp"; then
  # 镜像只提供字节；最终二进制必须匹配 Kubernetes 官方发布的 SHA256。
  curl -fSL --retry 1 --connect-timeout 10 --max-time 90 \
    "https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.37/deb/amd64/kubectl_${kubectl_version#v}-1.1_amd64.deb" \
    -o "$download.deb"
  # 只把指定成员读到 stdout，不安装 deb，不执行它的维护脚本。
  dpkg-deb --fsys-tarfile "$download.deb" | tar -xOf - ./usr/bin/kubectl > "$download.tmp"
fi
printf '%s  %s\n' "$(cat "$download.sha256")" "$download.tmp" | sha256sum -c -
mv -- "$download.tmp" "$download"
install -m 0755 "$download" .cache/bin/kubectl
GOBIN="$PWD/.cache/bin" go install "sigs.k8s.io/kind@$kind_version"
mkdir -p .cache/cni
GOBIN="$PWD/.cache/cni" go install github.com/containernetworking/plugins/plugins/ipam/host-local@v1.10.0
GOBIN="$PWD/.cache/cni" go install github.com/containernetworking/plugins/plugins/main/loopback@v1.10.0
{
  kubectl version --client --output=json
  kind version
  sha256sum .cache/bin/kubectl .cache/bin/kind
  CNI_COMMAND=VERSION .cache/cni/host-local
  CNI_COMMAND=VERSION .cache/cni/loopback
  sha256sum .cache/cni/host-local .cache/cni/loopback
} >build/logs/kubernetes-tools.txt
cat build/logs/kubernetes-tools.txt
