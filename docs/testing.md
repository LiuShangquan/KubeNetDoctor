# 转发、Service 共存与性能验收（v5）

状态：本文保留完整候选验收计划。M0–M6 的实际功能用例、DaemonSet/API 故障实验及 M7 短时性能实验已执行，证据索引见 [实施状态](implementation-status.md)。不能把阶段记录中的“通过”扩展为下表所有组合已覆盖。

当前尚未完成：60 秒以上持续测量、8/32 流、TCP_RR/CRR、系统性 GRO/GSO 开关矩阵、容量边界压力与 Map 峰值、完整 VXLAN 路由/邻居/FDB 逐项故障、长时间 API 重连/Node 删除重建风暴、长期 soak。100 次 Pod 生命周期只验证本次选定资源回收，未证明所有 FD/RCU 峰值预算。M7 实际每流 3 秒、同节点每组 5 轮，跨节点每模式 3 轮；这是探索测量，未达到原候选性能验收时长。见 [M7](experiments/M7.md)。

## 1. 分层与证据

Go 单元/性质/race 测试验证端点计划、快照和并发管理；BPF 包向量测试验证解析与动作，但不代替真实 redirect/CT/NAT 路径。Linux netns 验证 CNI、转发和故障；真实 Kubernetes 验证 runtime、对象变化和 kube-proxy；独立内核节点用于正式跨节点行为和性能。

采用现成 socat/nc、iperf3、netperf、tcpdump、bpftool、iproute2、conntrack 等工具。没有独立 TCP 收发程序任务；异常包用测试工具或固定包向量产生。

## 2. 核心矩阵

| ID | 场景 | 通过证据 | 阶段 |
|---|---|---|---|
| ENV-01 | /32、网关、宿主 /32 路由与静态邻居 | Linux 基线双向可达，TTL/源地址符合三层模型 | M0/M1 |
| ENV-02 | TC redirect 到目标 veth | 目标 egress 计数命中；该 hook 注入 DROP 时接收端确实失败 | M0 |
| ENV-03 | CT kfunc、FIB、checksum、BTF 与加载 | 真内核运行、verifier 输出；缺能力关闭 fastReady | M0 |
| CNI-01 | ADD/DEL/CHECK/VERSION | 标准结果及实际 /32、路由、邻居匹配；host-local 租约正确 | M1 |
| CNI-02 | 并发 ADD、重复 DEL、netns 已消失 | 无重复地址、无误删共享资源、可重试 | M1 |
| CNI-03 | 分配后/建链后/登记后失败，SIGKILL | 事务残留可识别，回滚或后续 DEL 完成，不提前复用活跃 IP | M1/M3 |
| CNI-04 | prepare/commit 丢回复，PodIP 尚未回写 | 一份附件/租约，不循环等待，不提前启用链路 | M3 |
| FWD-01 | 同节点 TCP/UDP 双方向 | redirect_requested、delivery_seen、实际收包三方一致 | M2 |
| FWD-02 | Native 跨节点 TCP/UDP 双方向 | 两节点入口/目标 hook 命中，源 IP 保留，每节点 TTL 只减一次 | M4 |
| FWD-03 | TTL 1、options、IPv4 分片、ICMP | SLOW，符合 Linux 基线；没有重复扣 TTL 或部分 MAC 改写 | M2/M4 |
| FWD-04 | 未解析邻居、超 MTU、无路由/黑洞/禁止路由 | 明确回退理由，内核执行正确的邻居/ICMP/拒绝行为 | M2/M4 |
| FWD-05 | 非法头、越界、非线性 skb、校验和错误 | 不越界、不误转，错误有可区分计数 | M2 |
| FWD-06 | 源地址伪造、输出接口/附件 epoch 失配、未知来源、非 host 单播 | 不进入非法快路径；明确拒绝不能借 SLOW 绕过 | M2/M4 |
| FWD-07 | GSO/GRO 开关、大 skb | 初版正确回退；吞吐报告给出路径比例，不能全慢路径冒称加速 | M2/M7 |
| CTRL-01 | Pod/Node 更新、乱序队列、缓存失联、容量满 | 最新计划不被旧任务覆盖，不发布半快照，关闭资格有证据 | M3 |
| CTRL-02 | Agent 重启、Map ABI 不匹配、节点 boot 改变 | 不信任旧 ifindex；区分 baseline 可用与 fastReady | M3/M5 |
| LIFE-01 | Pod IP/ifindex 复用、100 轮创建删除 | 不使用过期附件/快照投递；路由/邻居/IPAM/程序/Map/FD 无持续残留；在途 IP 复用歧义见边界文档 | M5/M7 |
| ACL-01 | 一对 Pod 的 TCP/UDP 双向拒绝，含合法 IPv4 分片 | FAST 与 SLOW 都经过相同目标门禁；分片不能绕过 IP/protocol 匹配 | M5 |
| ACL-02 | 无效 JSON、更新失败、附件紧急阻断 | 旧完整规则保留，新配置不标已应用；阻断需读回 | M5 |
| VX-01 | 隧道 Linux 基线、BPF 输出与解封装入口 | 内外层地址/VNI/UDP 正确，实际经过目标 hook | M6 |
| VX-02 | 路由/邻居/FDB 分别缺失、隧道端口阻断、MTU | 可定位具体阶段；回退不掩盖功能缺失 | M6 |

Service 的 SVC-01 至 SVC-08 定义于 [Service 合同](service-compatibility.md)，包括 ClusterIP、本地/远端后端、hairpin、DNS、NodePort、反向 NAT、后端变化和 CT 错误。M4 必须完成 Native 范围，M6 重复隧道范围。

## 3. 快路径必须有可观察证据

连接成功只能证明可达。验收 FAST 需要源端 redirect 请求、目标 egress 观察、接收端结果，以及必要的 MAC/TTL 抓包一致。helper 返回 REDIRECT 不等于驱动投递成功；不能把源端计数当作最终收包数。

在人工冻结拓扑、预热邻居、无旧 CT 的专用新连接实验中，选定的正常 IPv4 TCP/UDP 包应经过预期快路径；不符合资格的报文逐类确认 SLOW 理由。验收先做确定的包向量与受控流量，再讨论一般负载的比例。

只要某个直接连接曾走内核并建立 conntrack，本版保守 guard 就可能让其后续报文持续走 SLOW；这是设计行为。实验使用新的连接 tuple，并记录 CT 状态，不通过清空共享 conntrack 伪造性能。

每次实验保存配置、源代码 commit、版本、拓扑、snapshot generation、对象 UID 与时间。规则生效后测试和传播期测试分别进行；跨节点不假设全局原子发布。

## 4. 性能对照

| 组 | 路径 | 比较目的 |
|---|---|---|
| B0 | 相同三层拓扑，纯 Linux 路由，不挂项目 TC 程序 | 网络基线，仅在隔离测量环境使用 |
| B1 | 挂入口/目标检查，但转发始终 SLOW | hook/检查开销 |
| B2 | 完整快路径，包含 CT guard、FIB 与目标检查 | 实际交付实现的成本和收益 |
| B3 | B2 基础上的单项有证据优化 | 同语义对照 |

native 与 VXLAN 各先做 B0/B1/B2；ACL 开/关分别成组。不能拿关闭 CT guard 的特殊程序作为正常 Service 共存版性能，也不能把减少检查当作等价优化。

候选负载：iperf3 TCP 1/8/32 流、UDP 固定速率与包长；TCP_RR 请求响应、TCP_CRR 新连接；固定 CPU/MTU/offload/TCP 参数和服务进程。预热 10 秒、测量至少 60 秒、独立重复 5 次，交替或随机 A/B 顺序。不同负载按组报告。

同步记录吞吐、丢包、工具能真实提供的延迟分布、CPU/softirq、Agent RSS、Map 内存、快/慢路径比例与原因、规则收敛时间。只得到均值不能伪造 p99；应用块大小和 skb 数不等于线上帧大小和数量。

至少完成一次“剖析→假设→单项修改→相同条件复测→结论”；收益不显著也是可接受的真实结论，不预设 eBPF 必然更快。

## 5. 候选资源与运行门槛

规模候选为 64 本地 Pod、256 全局 Pod、32 节点元数据、4096 snapshot 条目、256 有向 ACL。后两种控制面压力规模不等于拥有同数量真实机器。初始预算 Agent RSS 256 MiB、项目 Maps 总峰值 64 MiB、ring 1 MiB，M2/M3 实测后冻结。

正常稳态更新候选观察上界 5 秒，必须带硬件/规模/轮询误差；这是待验证目标。关键误投、ACL 绕过、反向 NAT 跳过零容忍；允许正确回退，但回退过多必须在性能与限制里体现。

快路径开关及紧急降级的既有连接行为要测试；不承诺无损热切换。默认以固定模式、新连接开展基线对照。正式报告注明 VM 宿主资源共享；kind 不作为独立节点性能证据。

## 6. 结果归档

后续在 `docs/experiments/<编号>/` 保存环境、命令、manifest、原始输出、统计方法、抓包摘要、结论与限制。区分已运行、未运行、失败、环境阻塞。故障注入只影响隔离环境和自有资源，并包含恢复步骤。
