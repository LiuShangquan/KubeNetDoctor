# 关键技术决策

日期：2026-11-16。状态中的“选定”表示当前设计方向，不代表代码已经实现或验证。改变以下决策时记录原因、替代方案与受影响的测试，不同时维护多套未经验证的实现。

| 编号 | 决策与理由 | 状态与验证 |
|---|---|---|
| ADR-01 | 以自研 CNI、Go 策略编译和 TC eBPF 访问控制为核心。策略是串起控制面和逐包处理的开发载体；诊断只保留必要工具。 | 选定；依据用户目标和容器网络 JD。见 [岗位映射](jd-alignment.md)。 |
| ADR-02 | 先使用经典 TC clsact/direct-action，挂 host-side Pod veth 两个方向。该位置便于看到 Pod 的内层报文并决定 PASS/DROP。暂不引入 XDP 或第二套 hook 后端。 | 选定；M0 验证真实方向、动作、挂接与清理。机制参考 [Cilium BPF 文档](https://docs.cilium.io/en/stable/reference-guides/bpf/progtypes/)。 |
| ADR-03 | 基础转发复用 Linux bridge/IP 路由；R2 增加 routed VXLAN 的控制，不自行写封装。先完成固定 native 和固定 VXLAN 两种模式。 | 选定；M5/M6 分别验收，不含自动混合选路或在线切换。见 [拓扑](network-topology.md)。 |
| ADR-04 | CNI 从 1.0.0 的 ADD/DEL/CHECK/VERSION 开始；复用 host-local，地址全局唯一性由互不重叠的 Node PodCIDR 提供。 | 选定；M1 验证失败回滚、并发和地址回收。依据 [CNI 1.0](https://github.com/containernetworking/cni/blob/spec-v1.0.0/SPEC.md)、[host-local](https://www.cni.dev/plugins/current/ipam/host-local/)。 |
| ADR-05 | 第一版只适配实验 Namespace、普通 Pod、IPv4 直连 TCP 的 Ingress NetworkPolicy 子集。选择器和并集语义严格实现；未支持字段显式报错。 | 选定；M3/M5 逐项测试，不宣称全量 NetworkPolicy 合规。见 [语义表](datapath-and-policy.md)。 |
| ADR-06 | CNI 与本节点 Agent 使用受限 Unix socket 上的 HTTP/JSON；以附件事务键实现重试恢复，不额外引入 gRPC、数据库或集中服务。 | 选定；M5 验证响应丢失、重复请求和进程中断。接口见 [架构](architecture.md)。 |
| ADR-07 | 端点核验要求可信 runtime Pod UID；ADD 使用本次分配 IP，不能等待 Pod.status.podIP。身份不以 IP、Pod 名称或 ifindex 单独决定。 | runtime 参数是待验证条件；M0 核查，若不满足先更新适配决策，不能绕过身份检查。 |
| ADR-08 | 规则与必要身份编译进同一 immutable inner map，以 map-in-map 引用切换发布；一次 BPF 执行取一次引用。 | 选定；M3 检查 ABI/容量兼容、更新失败和持续流量。节点内引用替换不等于全局事务。参考 [Linux map-in-map](https://docs.kernel.org/bpf/map_of_maps.html)。 |
| ADR-09 | TCP 状态拟用普通 HASH、每项 spinlock 和 BPF timer。状态表满拒绝新授权，不通过 LRU 驱逐活跃连接；用户态不无条件删除“刚才读到过期”的项。 | **候选技术门禁**；M0 先验证 Map/锁/timer 支持，M4 验证初始化、回收及并发。失败则更新此决策，不假装兼容。约束见 [Linux BPF UAPI](https://raw.githubusercontent.com/torvalds/linux/v6.6/include/uapi/linux/bpf.h)。 |
| ADR-10 | 普通策略变更影响新连接，已批准连接可持续至关闭或期限；端点删除、身份失效和异常隔离优先于旧授权。 | 选定；M4/M5 测试。流状态是有限授权状态，不是完整 TCP conntrack。 |
| ADR-11 | 内核、runtime、库和工具版本在 M0 实机验证后固定。6.6/6.8 系列只是候选，未声称整个系列兼容。 | 待环境确认；记录实际补丁版本与来源，禁止使用“最新版”作为复现参数。 |
| ADR-12 | 使用现成工具产生流量，直接从 netns/veth/TC 实验切入，不安排独立 TCP 收发程序练习。 | 用户明确要求；TCP 状态机制在实际数据面代码中讲解和验证。 |

决策更改模板：原决定 → 新证据 → 新决定 → 功能/工作量影响 → 新增或修改的验收。没有证据时保留清楚标注的候选，不提前写“已验证”。
