# 实施状态

用户于 2026-11-16 明确要求自主推进整个项目，最终再逐步学习。因此连续执行各阶段，通过验收后自动进入下一阶段，不再等待逐阶段学习确认。保留原设计的正确性要求与真实结果记录。

| 阶段 | 状态 | 证据 |
|---|---|---|
| M0 | 内核机制验收通过 | 真实 redirect、目标 egress、CT 双方向、DNAT、hairpin SNAT、错误回退与负面对照；[记录](experiments/M0.md) |
| M1 | 最小 CNI 验收通过 | [真实 CNI/并发/崩溃恢复记录](experiments/M1.md) |
| M2 | 同节点原型验收通过（R0） | [真实转发、NAT、输入校验与目标门禁](experiments/M2.md) |
| M3 | 真实 Kubernetes 集成通过 | [独立内核双节点、runtime UID、Service/DNS 与 Agent 重启](experiments/M3.md)；已追加 DaemonSet 部署 |
| M4 | 验收通过（R1） | [Native 快路径与 Service 门禁](experiments/M4.md) |
| M5 | 声明的功能用例通过 | [ACL、离线 DEL、100 次 runtime 生命周期和身份漂移/IP 复用](experiments/M5.md) |
| M6 | 声明的功能用例通过 | [VXLAN 基线、快转、抓包/MTU、Service 与 ACL](experiments/M6.md) |
| M7 | 短时测量与优化评估完成；长时/规模测量未完成 | [B0–B3、内核耗时、Native/VXLAN 原始记录与限制](experiments/M7.md) |

环境与依赖准备已完成，见 [检查报告](environment.md)。最终部署与恢复验证见 [DaemonSet/API 故障记录](experiments/deployment.md)。各阶段“通过”仅指链接中实际执行的用例，不表示 [完整候选验收矩阵](testing.md) 的每个组合均已执行。

当前资源：Windows 约 32 GiB 内存；Ubuntu WSL2 为构建和 namespace 实验环境，两个 QEMU/KVM VM 各 2 vCPU/4 GiB、独立内核，用于 Kubernetes 跨节点功能验证。两者仍共享宿主硬件，不能作为物理多机性能证明。

已安装 Go 1.26.8、Clang 21.1.8、bpftool 7.7、QEMU 10.2.1 等；已下载并验证 Linux kubectl/kind、host-local/loopback。Ubuntu 的 Docker Desktop integration 未启用，Windows Docker/kind 可用。

## 尚未完成的验证

- 未做 60 秒以上多轮、不同并发/包长/ACL 开关的完整性能矩阵、长期 soak、容量上限与 Map 峰值内存测试。
- 未覆盖 kube-proxy 原生 nftables/IPVS、IPv6、多网络、任意 NAT/CT zone/Service Mesh、所有 NodePort 流量策略。
- 有一次未定位原因的 guest kernel panic；恢复后功能回归通过，不能据此认为原因消失。详见 [环境故障](experiments/environment-incidents.md)。
- 交付为可运行的网络研发原型及学习材料；不声称生产级 CNI、高性能收益、千节点或 GPU/RDMA 项目。
