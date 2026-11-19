# 数据面、策略编译与连接状态

状态：设计候选，未实现。首个目标是 Linux amd64、IPv4、普通 Pod 的 TCP 直连。本文是规则语义、Map 与状态机的唯一详细定义；环境参数需在 M0 验证。

## 1. Hook 与执行顺序

使用 host-side Pod veth 的经典 TC clsact/direct-action：

```text
Pod 发出 → host-veth ingress: 源身份核验、出站发起/返回流状态
 → Linux bridge/IP 路由或 VXLAN
 → 目标 host-veth egress: 目标端点入站策略、返回流状态
 → Pod 收到
```

同节点、跨节点都必须走目标 veth egress。TC 方向与 Pod 方向相反的位置需要通过实验确认。[TC BPF 说明](https://docs.cilium.io/en/stable/reference-guides/bpf/progtypes/)

受管端点一次执行的顺序：

1. 读取 ifindex binding，确认本次挂接的 endpointID/epoch 与状态。
2. 读取端点 Pending/Quarantined/Deleting 标志，暂不把它误用为所有协议 DROP。
3. 安全解析报文；核验源 IP/端点归属、协议和长度；对 TCP 应用端点门禁，优先于任何例外或旧流授权；其他协议按处理表执行。
4. 获取一次 policy snapshot 引用，所有快照内查表使用此引用。
5. 查询本地端点的流状态，核对身份、时间和状态转移。
6. 若为新的入站 SYN，按当前规则判断；若为本地发出的新 SYN，按入站策略项目的边界建立返回流授权。
7. 记录原因计数并返回 TC_ACT_OK 或 TC_ACT_SHOT。

没有规则缓存意味着未经证实的情况，不能据此直接放行受管 TCP。非受管端点不挂本项目策略程序；其基础网络不受策略模块接管。

## 2. 报文处理表

| 报文 | 处理 |
|---|---|
| Ethernet + IPv4 + TCP | 在长度/身份检查通过后进入策略与状态机 |
| IPv4 options | 按 IHL 计算 L4 偏移、严格检查范围；不解析业务含义 |
| IPv4 分片（MF 或非零 offset） | 受管端点拒绝并计数，不做重组 |
| IPv4/TCP 截断、IHL/DOFF 非法 | 拒绝并计数 |
| SYN 携带数据/TCP Fast Open、simultaneous open | 首版不支持，明确拒绝并测试 |
| 合法 ARP | 基础网络放行；本地发出方向核对 sender IP/MAC 与附件，网关行为通过实验确认 |
| IPv4 ICMP | 基础连通与 PMTU 调试放行并计数，不宣称受 TCP 策略保护 |
| 非分片 IPv4 UDP/SCTP 等 | 显式旁路并计数，表示未实施这些协议的策略，不宣称默认拒绝涵盖它们 |
| IPv6/VLAN/未知 EtherType | 受管实验端点首版拒绝并报告不支持；非受管系统端点不受影响 |

端点 Quarantined 是明确的 TCP 隔离模式，并非“所有协议均隔离”。报表与测试必须使用这个名称，不能笼统称为网络完全隔离。

读取 skb 时不能假设全部 TCP 头在线性区域。使用验证过的 skb 读取/拉取 helper，检查每次返回；可能改变缓冲布局的 helper 调用后重新获取指针与边界。锁内不调用这些 helper。GSO/GRO 打开和关闭均需验证。

不修改 IP、端口或负载；首版不自研校验和重算，也不把 offload 下抓包显示的未完成校验和当作坏包。计数单位为 hook invocation/skb，不是物理帧数。

## 3. NetworkPolicy 子集与默认值

控制范围仅为显式配置的实验 Namespace；来源标签可以引用集群内的普通 Pod。读取的是 API 默认化后的对象，不能要求从对象中判断用户 YAML 是否显式写过一个已被默认化的字段。

| 字段/情况 | 支持的语义 |
|---|---|
| policyTypes 为 Ingress | 支持；默认化后为 Ingress 也接受 |
| 包含 Egress | 不支持，按异常策略处理，不能忽略 |
| spec.podSelector.matchLabels | 在策略所属 Namespace 选择目标；空 selector 匹配该 Namespace 全部 Pod |
| ingress 缺省或空列表 | 该策略提供隔离，不增加任何允许规则 |
| ingress 中一个空规则 `{}` | 该规则允许所有来源/端口；在本项目内仅验证普通 Pod TCP 范围 |
| rule.from 缺省或空列表 | 本规则不限制来源；不是拒绝所有来源 |
| rule.ports 缺省或空列表 | 本规则不限制端口；本项目仅实施 TCP，不声称覆盖所有协议 |
| peer.podSelector，未指定 namespaceSelector | 在策略 Namespace 匹配来源 Pod |
| peer.namespaceSelector | 在匹配的 Namespace 内匹配来源 Pod；空 selector 表示所有 Namespace |
| 同一 peer 的两个 selector | AND；不同 peer 之间 OR |
| 数字 port + TCP | 支持；省略 protocol 以 API 默认 TCP 处理 |
| port 缺省、protocol TCP | 支持全部 TCP 端口 |
| 多 rule / 多 policy | 各允许集合求并集；不存在优先级和显式 deny 规则 |
| 未被入站策略选中 | 支持范围内的新入站 TCP 不隔离 |
| matchExpressions、ipBlock、命名端口、endPort、非 TCP policy port | 首版不支持 |

上述空列表/省略含义核对 [Kubernetes networking/v1 类型定义](https://raw.githubusercontent.com/kubernetes/api/master/networking/v1/types.go)。实现必须固定到所选 Kubernetes release 的源码，而不是依赖浮动 master。

“所有来源”在标准语义中不限于普通 Pod；本项目对外部、hostNetwork、Service/NAT 等流量没有完整兼容性承诺。选定环境中本机 Node 的已配置地址到本机 Pod 的流量做明确例外，并测试 kubelet 探测：解析及源检查、端点门禁通过后，已确认本机 Node 地址的 TCP 入站及对应出站方向显式旁路策略/流表，计为 node_exception。例外不使用任意 Node CIDR 泛化；它依赖受管 Pod 源地址校验和可信其余实验环境，不能仅凭一个看似 NodeIP 的报文就宣称有认证身份。

策略编译错误、未知字段或容量超限：不发布不完整允许表。先按 architecture.md 对已知受影响目标进行 TCP 隔离；目标无法可靠确定时隔离受管 Namespace。此行为是本项目失败策略，不是 Kubernetes 标准中的显式 deny。

## 4. 编译算法与查询次数

编译器输入为不可变的 Pod/Namespace/Policy/本地附件集合；输出是 `CompiledSnapshot`，含 generation、输入摘要、身份项、端点隔离项、允许项和限制统计。

第一版按全量确定性重算：校验支持字段 → 解析 selector → 确定目标集合 → 展开来源集合与端口 → 合并去重 → 容量检查。对同一输入应生成相同内容摘要；只改变顺序不应改变结果。

允许规则逻辑键：`(targetEndpointID, targetEpoch, sourcePeerToken or ANY, TCP, destinationPort or ANY)`。数值端口 0 与 ANY 使用独立标志区分，不用有效值承担哨兵语义。

单次新入站匹配最多尝试四种组合：精确来源/精确端口、精确来源/任意端口、任意来源/精确端口、任意来源/任意端口。之后根据目标是否隔离决定；计数不参与允许规则。

身份项与端点项也存于同一个 snapshot，使用 tagged key 区分，不用多个分别切换的 Map 冒充事务。临时 ACL 阶段可以直接提供规范化规则，但 Kubernetes 阶段不绕过选择器编译。

## 5. BPF Map 与 ABI 合同

以下为逻辑结构，C/Go 字节布局将在 M2 固定并以编译断言和 golden bytes 测试核对；不能把此表当作已存在库类型。

| Map | 类型候选 | 键/值 | 所有者 |
|---|---|---|---|
| endpoint_bindings | HASH | host ifindex → endpointID、epoch、PodIP、Ready/Blocked、配置摘要 | Agent 写，BPF 读 |
| active_policy | ARRAY_OF_MAPS，1 槽 | 0 → 当前完整 snapshot | Agent 切换，BPF 读 |
| policy_snapshot | HASH，immutable | tagged key → 身份/端点标志/允许项/版本 | Agent 建表，BPF 读 |
| flow_state | HASH | 本地端点 + epoch + 本地/远端 IP/port + TCP → 授权状态、PeerToken、ISN、时间、锁/定时器 | BPF 写，Agent 只读诊断 |
| reason_counters | PERCPU_ARRAY | 固定 reason ID → skb/byte 计数 | BPF 写，Agent 汇总 |
| debug_events | RINGBUF，可关闭 | 有限的状态/拒绝事件 | BPF 写、Agent 有界消费 |

地址字段用固定 4 字节网络序；端口明确使用网络序；endpoint/generation/时间采用固定宽度整数，在首个 little-endian 平台按生成绑定读写。显式保留 padding 与 ABI version，不用 Go 默认结构体布局猜 C 内存布局。

候选上限：每节点 64 受管端点、集群 256 普通端点身份、单快照 65,536 条目、流表 32,768 项、调试 Ring Buffer 1 MiB。超限拒绝新编译/新状态，测试端主动缩小容量触发错误路径。Map 实际内存（含预分配、对齐和 per-CPU 倍数）必须测量后确定最终预算。

Agent 主动持有当前快照与一个构建中的快照；旧快照释放用户态 FD/pin，内核在引用消失后回收，因此快速更新时的瞬时内存可能超过两份，必须计入峰值预算并限制发布频率。所有 inner map 采用兼容的类型、键值大小和固定容量，M2/M3 实测 outer map 模板约束。单次 BPF 执行只取一次 inner map 引用。[Linux map-in-map](https://docs.kernel.org/bpf/map_of_maps.html)

## 6. TCP 授权状态机

本项目不重实现 TCP 的拥塞控制、重传定时器或完整窗口校验。状态机只解决“这条返回流是否属于本端点发起/已允许的连接”，不能宣传为完整 conntrack 或生产防火墙。

流键始终以本地 Pod 为方向基准，因此同一连接的入/出报文可归一到同一项。不同本地端点各有自己的项；跨节点不共享 socket ID。值包含 initiator=LOCAL/REMOTE、授权时的策略 generation、PeerToken、双方初始序号、状态、FIN 位与时间。

| 当前状态 | 报文/条件 | 动作与下一状态 |
|---|---|---|
| 无记录 | 本 Pod 发出的纯 SYN | 源身份正确且状态资源可用则创建 PendingLocal；入站专用策略不限制该出站发起 |
| 无记录 | 发往本 Pod 的纯 SYN | 当前入站规则允许才创建 PendingRemote；拒绝则 DROP |
| 无记录 | ACK/数据/SYN-ACK | 不凭 ACK 建立授权；DROP，并允许业务重新建连 |
| PendingLocal/Remote | 同方向同 ISN 的 SYN 重传 | 保留同一代状态，不刷新硬性半连接寿命 |
| PendingLocal/Remote | 反方向 SYN-ACK，ACK=发起 ISN+1 | 记录响应 ISN，进入 SynAckSeen |
| SynAckSeen | 发起方向最终 ACK，确认响应 ISN+1；序号符合无 SYN 数据条件 | 进入 Established；最终 ACK 可带正常数据 |
| SynAckSeen/Established | 对应握手重传 | 允许已记录的握手序列，不重置到新连接 |
| Established | 同键、身份一致的普通 ACK/数据 | 放行并更新时间；不声称完成所有 TCP 序号窗口检查 |
| Established | FIN | 标记半关闭，允许合法另一方向数据/确认；进入 Closing |
| Pending/Established/Closing | 属于该项的 RST | 放行后进入终止/待清理，不允许它创建新授权 |
| Closing | 重传 FIN/ACK、未关闭方向流量 | 在关闭期限内处理，期限后拒绝旧流 |
| 任意 | epoch/PeerToken 失效、端点隔离、硬超时 | 失效并 DROP；新连接重新评估 |

已存在项收到不同 ISN 的新 SYN，不把旧授权直接转移：旧项未关闭/过期时拒绝冲突；过期后按新连接重新评估。相同五元组的快速复用可能需要等待清理，这属于明确限制。

候选时间：半连接硬上限 30 秒、Established idle 300 秒、关闭保留 30 秒、授权硬寿命 3600 秒。它们是可配置实验参数，不是 Linux TCP 默认值；长连接会受硬寿命限制。测试时缩短时间，不把等待一小时作为日常检查。

普通策略变更对新连接生效，旧连接保留原授权至关闭/超时；端点删除、身份冲突和异常隔离立即使旧项不能通过。正常规则更新的既有连接行为属于实现选择，需要在测试与限制文档写明。[NetworkPolicy 对既有连接的说明](https://kubernetes.io/docs/concepts/services-networking/network-policies/)

## 7. 并发、过期与容量

选择普通 HASH + 每项 bpf_spin_lock，先验证此类型与 helper 的目标内核支持。新项使用 NOEXIST 插入；只有创建成功者初始化状态，竞争者重新查找。初始化期间保持不可授权，timer 初始化与启动成功后才开放状态；失败不能留下永久有效项。锁内只更新已读出的字段，不进行 helper 调用或再次 Map 查询。[Linux BPF 锁约束](https://raw.githubusercontent.com/torvalds/linux/v6.6/include/uapi/linux/bpf.h)

过期回收选择 **每项 BPF timer** 的候选实现，并作为 M0/M4 的独立技术门禁。时间读取在锁外；回调持锁复查 lastSeen/硬截止时间，只有仍过期才标记 Tombstone，解锁后删除。其他报文看到 Tombstone 不得复活或替换该键，等待删除后重新创建。活跃状态变化则重新安排定时器。初始化/启动 timer 失败时该项保持不可授权并清理。

不在用户态采用“读取过期项然后删除”的无条件算法，避免删掉刚被报文刷新的项；也不把 LRU_HASH 与 spinlock 的兼容性未经验证就视为成立。若 timer/helper/verifier 门禁失败，先修改 ADR 或选择已支持的基线，不能悄悄降级为无同步状态。

普通 HASH 满时拒绝依赖新状态的新连接，已有状态继续处理。无需主动 LRU 驱逐活跃连接；不会为了释放槽位删除任意合法连接。回收延迟和拒绝计数要可见；节点 Agent 不读取事件流来代替内核逐包授权。

端点失效先在 binding/snapshot 门禁拒绝其报文，流表中的物理清理可延后；这样清理耗时不意味着授权继续有效。对远端 API IP/UID 变化存在传播窗口；本项目保证收到并发布新身份后不沿用旧 UID 授权，不承诺节点间即时撤销。

## 8. 调试与需要验证的边界

reason ID 至少区分：policy_allow、policy_deny、return_allow、node_exception、pending_endpoint、unsupported_packet、bad_header、fragment、spoofed_source、unknown_identity、invalid_state、expired_state、state_capacity、state_init_error、quarantined。不要将程序丢弃、网卡丢包与业务失败混成一个计数。

调试事件包含 node/endpoint/epoch、方向、tuple、generation、reason 和内核时间；每秒候选最多 100 条，默认不逐包打印。Ring Buffer 满只损失调试信息，不能改变 PASS/DROP 决策。计数不承诺还原完整报文历史。

M0 需要验证 TC/Map/timer/lock 的能力；M2 固定 ABI 与报文表；M4 验证状态转移、并发和清理。没有这些证据，不声称“支持连接跟踪”“无损热更新”或“策略完整兼容”。
