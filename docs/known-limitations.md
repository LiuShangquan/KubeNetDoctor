# 支持范围与已知限制

更新于 2026-11-17。已实现的功能与实测用例见 [实施状态](implementation-status.md)。这里区分功能边界、已观察到的问题和未完成的验证。

## 1. 已交付范围

| 部分 | 已实现/实验范围 | 尚不支持或未验证 |
|---|---|---|
| CNI | Linux、IPv4、单网络/单接口普通 Pod；ADD/DEL/CHECK/VERSION；host-local | Windows 数据面、IPv6/双栈、Multus、分布式 IPAM |
| 接入 | routed veth、Pod /32、静态邻居和受管路由 | bridge、透明二层语义、任意现有宿主网络接管 |
| BPF | 已知 Pod 间 IPv4 TCP/UDP、明确 CT miss、FIB/邻居/输出约束成功时快转 | 全包型加速、完整路由栈、自研 CT/NAT、硬件卸载 |
| Native | 受控直连 underlay、允许 Pod 源 IP 的两节点实验 | 任意云 VPC、BGP/ECMP、混合自动选路 |
| VXLAN | 单 VNI 42、UDP 4789、静态邻居/FDB、MTU 1450 | 动态学习/EVPN、多租户隔离、在线无损模式迁移 |
| Service | kube-proxy iptables 模式、nf_tables 后端；ClusterIP、DNS、hairpin、限定 NodePort 与后端变更 | 原生 nftables/IPVS、所有 externalTrafficPolicy、DSR、Service Mesh、任意第三方 NAT/CT zone |
| ACL | 默认允许、按 UID 编译的有向 TCP/UDP Pod 对拒绝；本节点紧急阻断 | Kubernetes NetworkPolicy、标签/端口规则、完整原始 Service 身份、状态防火墙 |
| 部署/观察 | systemd 与专用 DaemonSet；kndctl 状态、原因计数、API 健康 | 通用安装器、运维平台、ring buffer 逐包跟踪、自动根因分析 |

Pod→外部和节点流量依赖 Linux/实验环境路由，本项目没有统一出网 NAT。hostNetwork 不经过 Pod veth 门禁。ACL API 没有 namespace 白名单；测试仅在 knd-test 中使用，root 调用者负责正确选取 UID。

## 2. 转发和身份约束

- CT kfunc 随内核/BTF/配置变化；版本号不足以证明支持。缺少能力时加载不依赖 CT kfunc 的基线 ELF，关闭快转，保留入口和目标检查。该降级有强制缺符号实验，但不能声称兼容所有内核。
- 仅明确 CT miss 才 FAST；hit/error 都 SLOW。直连连接曾经过 Linux 建立 CT 后，后续可能持续慢路。大 GSO/GRO skb、ICMP、options/分片、TTL/MTU 等特殊情况保留内核处理。
- FAST 可能绕过常规 Netfilter FORWARD 等处理；不保证继承任意宿主防火墙/QoS。本项目目标 veth 门禁有快慢路径实测。
- 快照完整一致，不等于路由、binding、Maps、多个节点间全局原子。控制面变化有传播窗口；不提供跨节点 ACL 同时生效承诺。
- UID/epoch/alias 防止错误使用旧附件；普通 IP 包不携带 UID，不能完全区分合法复用 IP 后的在途旧包。可信实验 underlay 不是密码学身份验证，也不是多租户安全边界。
- ACL 在目标 hook 使用实际看到的地址；SNAT 可能隐藏原始 Pod 身份。无效更新保留旧配置，不代表新增拒绝已生效。
- API 不可达时保留旧数据面，并拒绝需要 fresh UID 验证的新 prepare；长期失联期间远端对象可能过时。
- Agent 重启复用 ABI v2 pin 下的程序/Map；仅替换磁盘 ELF 不会在线替换已 pinned 程序。程序升级需受控迁移。模式切换脚本删除演示附件后重建，不保证既有连接无损。

## 3. 已观察到的问题

**一次 guest 内核崩溃，根因未确定。** node1 在 systemd 调用 openat 的路径检查中发生 general protection fault，堆栈含 path_is_under，Linux 7.0.0-43-generic。VM 恢复后功能测试继续通过，但没有最小复现、内核修复或原因证据，不能声称环境长期稳定，不能归因于 eBPF 或某一个命令。保留 [故障日志与说明](experiments/environment-incidents.md)。

**重启目录缺失问题已修复。** `/run/knd` 和 netns hostPath 改为 DirectoryOrCreate，恢复后 Agent 与应用 sandbox 能重新接入。仍未完成多次节点重启/断电/磁盘损坏矩阵。

**iperf3 线程退出被 guest AppArmor 拒绝。** 审计明确为 AppArmor signal deny；跨节点性能脚本只为两个临时 benchmark Pod 配置 Unconfined，未修改普通 Pod 或全局 AppArmor。这个测试例外不能作为生产安全配置建议。

## 4. 性能、规模与测试边界

短时同节点 B0–B3、BPF 耗时和 Native/VXLAN 测量已完成。**没有证据证明稳定吞吐收益**；B3 优化候选未采用。两台独立内核 VM 共享宿主 CPU/内存与 QEMU socket underlay，测量受背景负载影响；UDP 存在丢包。这些不是物理线速或生产集群容量数据。

M7 每流仅 3 秒，同节点 5 轮，跨节点 3 轮；未执行原计划每流至少 60 秒、8/32 并发流、全包长/ACL 开关矩阵、长期 soak。RTT 是 Python 应用往返时间；程序纳秒耗时是 BPF_STATS 采样；计数是 hook/skb，不是物理包数。不可互相替代或编造 p99。

编译限制为本地+远端端点共 256、节点路径 32、有向 ACL 256、快照 4096 项；这不是同规模集群认证。100 次 Pod 创建删除验证了本次接口/IPAM/journal/记录回收，不等于长期无任何资源泄漏；Agent RSS 和 Map 峰值预算未做压力验收。

本项目可以证明亲自实现并验证容器网络组件的能力，不能描述成千节点生产经验、GPU/RDMA/DPDK 研发或成熟 CNI 替代品。
