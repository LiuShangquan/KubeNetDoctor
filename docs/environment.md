# 开发环境检查与依赖

初次检查日期：2026-11-16；更新于 2026-11-17。工具链已经安装并通过实际编译/加载。此后已部署两个独立内核 QEMU/KVM 节点，运行自研 CNI/Agent/TC eBPF 与真实 Kubernetes。完整转发、Service、生命周期及 VXLAN 用例见 [实施状态](implementation-status.md)，下文基础探针结果保留为最初环境证据。

## 1. 已确认的环境

| 项目 | 实际结果 |
|---|---|
| Windows | Windows 11，Intel i9-14900HX，24 核/32 逻辑处理器，约 32 GiB 内存 |
| 开发 Linux | Ubuntu 26.04 LTS，WSL2，amd64 |
| 运行内核 | 6.18.33.2-microsoft-standard-WSL2 |
| WSL 内存 | 约 15.8 GiB；当前配置足够进行单机机制实验与少量 VM 实验 |
| BTF / cgroup | `/sys/kernel/btf/vmlinux` 可读；cgroup v2 |
| KVM | API 版本 12；实际执行 KVM_CREATE_VM 成功 |
| Docker Desktop | Windows 侧 Engine 29.7.2 可用；运行临时 Alpine 容器成功 |
| Windows kind / kubectl | 已有 kind v0.33.0、kubectl v1.36.1 |
| Ubuntu systemd | 检查时状态 running；iperf3 服务 inactive |

Ubuntu 当前未启用 Docker Desktop WSL integration，所以 Linux `docker` 命令尚不能连接 Desktop。当前内核实验直接运行在 Ubuntu；Docker/kind 集成可从已有 Windows 工具启动。正式独立内核节点可使用已验证的 KVM/QEMU。现有其他项目集群不作为本项目验收环境。

## 2. 已安装或下载的依赖

| 工具 | 实际版本/位置 | 用途 |
|---|---|---|
| Go | 1.26.8，Ubuntu `/usr/bin/go` | CNI、Agent、控制面 |
| Clang / LLVM | 21.1.8 | 编译 eBPF C |
| libbpf 开发包 | 1.6.3 | BPF helper 头文件 |
| bpftool | 7.7.0，使用 libbpf 1.7 | BTF、程序/Map、能力检测 |
| iproute2 | 6.19.0 | netns、veth、路由、TC |
| iptables | 1.8.11，nf_tables 后端 | 后续 kube-proxy/NAT 夹具 |
| QEMU | 10.2.1；另有 qemu-utils/cloud-image-utils | 独立内核 VM |
| kubectl（Linux） | v1.37.2，`.cache/bin/kubectl` | 后续集群访问 |
| kind（Linux） | v0.33.0，`.cache/bin/kind` | 可选集成工具；Linux Docker 集成尚未启用 |
| host-local / loopback | containernetworking/plugins v1.10.0，`.cache/cni/` | 复用 IPAM 和 loopback，支持 CNI 1.0 |
| 其他 | gcc、make、pkg-config、libelf-dev、conntrack、tcpdump、iperf3、ethtool、git、OpenSSH client | 构建和网络测试 |

项目 go.mod/go.sum 已固定 CNI v1.3.1、plugins v1.10.0、netlink v1.3.1、cilium/ebpf v0.22.0、Kubernetes 库 v0.37.2 等依赖，保留 Go 模块校验。

版本原始输出在 `build/logs/toolchain.txt` 和 `build/logs/kubernetes-tools.txt`。`.cache/`、`build/` 均被 Git 忽略，重建方式保留在脚本中。

## 3. 已执行的真实验证

| 检查 | 实际证据 |
|---|---|
| Go 构建/运行 | 编译 Linux amd64 程序并得到预期输出 |
| BPF 编译/加载 | Clang 生成 ELF/BTF；内核 verifier 接受 TC 程序与 Maps |
| helper / Map 支持 | SCHED_CLS、HASH、ARRAY、PERCPU_ARRAY、ARRAY_OF_MAPS、RINGBUF，以及 redirect/FIB/checksum/skb 读写 helper 可用 |
| 三层接入 | 两个 Pod netns + 一个节点 netns；/32 veth 接入，双向 ping 成功，TTL=63 |
| FIB 实际调用 | 3 次查询，3 次成功 |
| CT kfunc 实际调用 | 查询人工不存在的 UDP tuple：3 次 miss，0 次 error |
| TC egress 门禁 | 加入 DROP 后 100% 丢包；删除规则后恢复通信 |
| VXLAN 基础能力 | 在隔离 namespace 成功创建 VXLAN 设备 |
| IPAM | 分配两个不同地址；释放及重复 DEL 成功；租约残留为 0 |
| 资源清理 | 所有临时 network namespace 和私有 bpffs 挂载均已回收 |

原始命令、退出码、输出和计数位于 `build/environment/result.json`；精简可保留证据见 [环境结果](environment-results.json)。IPAM 单独结果在 `build/environment/ipam-result.json`。

**这些基础探针本身不证明完整数据面。** 后续真实 redirect/目标 hook、CT hit/反向 NAT、VXLAN 传包和 Kubernetes 实验另有 M0–M6 记录，不能把环境 PASS 与功能 PASS 混用。

首次探针加载发现：仅前置声明 `struct nf_conn` 不能满足当前 libbpf 对 kfunc 参数的类型匹配。现改为从运行内核 BTF 生成 `vmlinux.h`，重新编译加载通过。该头文件是构建产物，不手工复制内核结构体布局。

## 实际 Kubernetes 实验环境

两个 Ubuntu 26.04.1 VM，各 2 vCPU/4 GiB、独立 Linux 7.0.0-43-generic 内核；K3s v1.37.2+k3s1，containerd 2.3.7-k3s1。禁用 Flannel/内置网络策略，保留 kube-proxy iptables 与 CoreDNS。CNI 目录 `/opt/cni/bin`、`/etc/cni/net.d`；最终 Agent 为 v0.3 DaemonSet。underlay 192.168.77.11/12，PodCIDR 10.244.0.0/24 和 10.244.1.0/24，ServiceCIDR 10.96.0.0/12。

Ubuntu cloud image SHA256：`7b90593f5532b49b7e3fe3d2743f0d1f0d822e5b07cebcd8e00c0df8069ecab9`。K3s 与 airgap 镜像也由下载脚本核验官方校验和。两个 VM 共享物理宿主和 QEMU socket 链路，只提供独立内核功能证据，不等同于物理多机性能。

环境中发生过一次原因未明的 guest kernel panic，以及已修复的 hostPath 重启恢复问题；iperf3 的 AppArmor 限制已在临时 benchmark Pod 中处理。详见 [故障记录](experiments/environment-incidents.md)。

## 4. 下载与校验

WSL 的官方 Go 代理直连超时，Ubuntu 官方源下载较慢。本次使用：

- 临时 Ubuntu 镜像源配置，保留发行版 Signed-By 校验，不改系统源文件。
- 项目级 `GOPROXY=https://goproxy.cn,https://proxy.golang.org,direct`，保留 `GOSUMDB=sum.golang.org`。
- kubectl 从镜像包中只读取指定二进制，并与 Kubernetes 官方 SHA256 校验一致后才安装；没有执行该 deb 的维护脚本。

kubectl v1.37.2 SHA256：`c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4`。

Windows localhost 代理未自动传入 WSL 的提示仍可能出现；已验证的源与代理足以完成本轮工具下载。没有关闭 TLS 或 Go 模块校验。

## 5. 重跑方式

在 Windows PowerShell 的项目目录中检查：

```powershell
python scripts/wsl.py python3 /mnt/d/KubeNetDoctor/scripts/check-environment.py
python scripts/wsl.py python3 /mnt/d/KubeNetDoctor/tests/environment/check-ipam.py
```

`check-environment.py` 在独立 mount namespace 中建立临时实验，结束时清理自建资源。`check-ipam.py` 只使用临时租约目录。运行需要 Linux root，由 WSL 桥接脚本明确指定。

首次安装或重新准备依赖：

```powershell
python scripts/wsl.py env KND_APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ubuntu bash /mnt/d/KubeNetDoctor/scripts/bootstrap-linux.sh
python scripts/wsl.py env KND_KUBECTL_MIRROR=1 bash /mnt/d/KubeNetDoctor/scripts/download-tools.sh
```

在 Ubuntu 内开发时：

```bash
cd /mnt/d/KubeNetDoctor
source scripts/env.sh
go version
kubectl version --client
```

`env.sh` 只影响当前 Shell，不修改用户全局 Go 配置。新的项目集群、VM 镜像和容器运行时部署在对应集成阶段准备，不能把工具已安装等同于集群已部署。
