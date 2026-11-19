#!/usr/bin/env bash
# 使用 source scripts/env.sh 激活项目工具路径；不修改用户全局 Go 配置。
KND_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$KND_PROJECT_ROOT/.cache/bin:$PATH"
export GOPROXY="${GOPROXY:-https://goproxy.cn,https://proxy.golang.org,direct}"
export GOSUMDB="${GOSUMDB:-sum.golang.org}"
