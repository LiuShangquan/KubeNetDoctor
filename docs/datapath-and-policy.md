# eBPF 转发与辅助 ACL（v5）

状态：已实现的数据面合同；同节点、Native、辅助 ACL 和 VXLAN 的实测见 [实施状态](implementation-status.md)。主线是 TC eBPF 的 IPv4 三层转发。文件名保留；前版 NetworkPolicy 编译与 TCP 授权状态机已归档，不是本版阶段要求。

## 1. 三种结果与四类入口

程序只能明确选择：FAST（重定向）、SLOW（原样继续内核）、DROP（拒绝）。不满足加速条件可以 SLOW；源伪造、附件不合法、明确 ACL 拒绝不能借 SLOW 绕过。

源 Pod host-veth ingress、underlay ingress、VXLAN 解封装后的 kndvx0 ingress 共用解析/资格检查/FIB 转发函数，但各自有不同的来源范围。目标 host-veth egress 执行最终端点与 ACL 检查，永不递归转发。

使用 clsact/direct-action、`bpf_redirect(outIfIndex, 0)`；暂不引入 XDP、TCX、redirect_peer、tail-call 管线。M0 验证实际输出 hook，而不是推断所有 redirect helper 都有相同路径。[TC 机制](https://docs.cilium.io/en/stable/reference-guides/bpf/progtypes/)

## 2. 快路径资格与处理顺序

1. 验证入口设备所属角色；本地 Pod 核对 binding、Ready、源 IP/MAC。控制器状态未就绪时关闭加速；不可信本地附件不能直接放开。
2. 安全读取 Ethernet/IPv4 头，判断长度、IHL、分片、协议、TTL 与 skb/offload 状态。快转只接收发往当前入口预期 MAC 的单播 host 报文；广播、多播、PACKET_OTHERHOST 等不直接重定向。对 IHL=5 的候选校验 IPv4 头校验和，再检查 TCP/UDP 头边界；不把 L4 checksum offload 状态误认为 IPv4 头损坏。
3. 获取一次 immutable snapshot。源和目的都必须是已登记普通 Pod；排除 ServiceCIDR、节点/网关/外部地址。underlay/tunnel 入口只接受当前模式允许的远端来源和本地目标，不能成为任意转发器。
4. 执行本机 conntrack 只读查询：命中或错误均 SLOW；只有明确 miss 才继续。见 [Service 合同](service-compatibility.md)。
5. 调用 `bpf_fib_lookup`，按实际入口、地址、TOS、协议、端口和报文长度填充参数；首版 flags=0，不用 DIRECT 跳过 FIB rules、不用 SKIP_NEIGH。核对输出接口/下一跳与 snapshot 中本地端点或远端节点路径一致；本地输出还须核对当前 binding 的 ID/epoch 与期望附件一致。
6. FIB 非成功、邻居缺失、MTU 条件不满足或结果不在允许范围时 SLOW。黑洞/禁止路由也保留给内核执行其语义，不自行猜测成功下一跳。
7. 所有可回退条件检查完成后，进入报文修改阶段：保证待写头部可写，重新验证指针；TTL 减 1、修正 IPv4 头校验和、按 FIB 结果改写源/目的 MAC。
8. 返回 `bpf_redirect(outIfIndex, 0)` 的动作；记录 redirect_requested。它表示已提交重定向，不代表对端最终收到，实际交付用目标 hook 和接收端确认。

`bpf_fib_lookup` 复用内核路由和邻居表，程序自己组织资格判断、报文修改和输出动作；并非自行实现完整路由栈。helper 合同见 [Linux BPF UAPI](https://raw.githubusercontent.com/torvalds/linux/v6.6/include/uapi/linux/bpf.h)，实际支持需 M0 验证。

## 3. 修改与回退的硬约束

- SLOW 返回前报文不能已经减少 TTL 或部分改写 MAC。修改后的失败走 DROP 并计数，不能把半修改报文再交内核。
- 对有 options、TTL≤1、需要分片/ICMP 错误的包保留 Linux 路径，由内核处理。快路径每次三层转发只减一次 TTL。
- 不修改 IP 源/目的、TCP/UDP 端口与负载；因此不做 L4 NAT 校验和更新。IPv4 头校验和更新仍是必做代码。
- 使用可验证的 skb 读写 helper；非线性缓冲不是读取失败后默认放行的理由。格式非法与格式合法但快路径不支持分开计数。
- 初版大 GSO/GRO skb 保守走 SLOW，M7 根据实测决定是否优化；吞吐报告必须同时给出 fast/slow 比例。
- 从同一代 snapshot 读取快转资格；FIB 与 Map 并非原子事务，结果不匹配就保守继续内核。目标端点检查独立执行。

## 4. 报文类别

| 报文/条件 | 转发程序动作 |
|---|---|
| 合格的 IPv4 TCP/UDP Pod→Pod，CT miss，FIB 成功 | FAST，目标仍执行最终检查 |
| ServiceCIDR、节点/网关、外部或未知端点 | SLOW |
| 本机 CT hit / 查询错误 | SLOW |
| 合法 ARP、ICMP、其他 IPv4 协议 | SLOW，沿用 Linux 基础行为 |
| 合法 IPv4 options/分片/大 GSO skb、TTL≤1 | SLOW |
| FIB/邻居/MTU 不满足快路径 | SLOW，记录具体原因 |
| 本地源 IP/MAC 与附件不符，已删除/隔离端点，非法头 | DROP |
| VLAN、IPv6、未知 EtherType | 不进入快路径；受管实验 Pod 的不支持类型按固定配置拒绝，underlay 上原样交内核，不破坏宿主其他业务协议 |

入口校验与目标检查应用于本 CNI 创建的普通 Pod；hostNetwork 不在 Pod veth 路径中。测试只对 knd-test 的 UID 配置 ACL；接口本身没有 namespace 白名单，root 调用者须选择正确 UID。

## 5. Maps 与控制面发布

| Map | 已实现类型 | 内容/所有者 |
|---|---|---|
| bindings | HASH，256 槽 | host ifindex→endpointID、epoch、PodIP/MAC、Ready/Blocked；Agent 写 |
| active_snapshot | ARRAY_OF_MAPS，1 槽 | 当前完整快照引用；Agent 切换 |
| snapshot_template / 动态 inner map | immutable HASH，tagged keys，4096 槽 | IP→PodUID token/本地端点或节点；节点路径约束；ACL；generation |
| runtime_config | ARRAY | fastReady、模式、ServiceCIDR、接口角色/配置版本；更新有序且保守关闭 |
| reason_counters | PERCPU_ARRAY | FAST 请求、各类 SLOW、DROP、目标交付 hook 计数 |

编译器约束：本地与远端端点合计最多 256、节点路径最多 32、有向 ACL 最多 256、快照最多 4096 项。拒绝超限输入，不截断表。64 本地 Pod 是早期候选验证规模，未做同规模压力认证。没有实现 ring buffer 调试事件。ABI v2 的 key/value/config 分别为 16/64/32 字节；固定宽度、网络序、padding 有 C 静态断言和 Go golden bytes 测试。

Agent 构建完整新快照后切换引用；一次 invocation 只取一次引用。旧 Map 引用释放后的实际回收由内核决定，瞬时内存可能超过两份，发布频率和峰值预算需测试。[Map-in-map](https://docs.kernel.org/bpf/map_of_maps.html)

首版没有自研 flow_state、TCP 授权缓存或 BPF timer；不把只读 CT 查询包装成自研 conntrack。

## 6. 目标端点检查与辅助 ACL

host-veth egress 统一检查当前附件 Ready、目标 IPv4 地址是否属于该附件，再按当前 ACL 快照决定是否投递。这个检查覆盖 FAST 和 SLOW；测试必须证明不能通过改变路径绕过。

M5 只实现 **无状态、默认允许的实验性 Pod 对拒绝 ACL**：配置绑定明确的源/目标 Pod UID 和 TCP/UDP 协议；需要双向隔离时编译成两条有向拒绝项。配置来自本项目有版本的本地 JSON，经 Agent 校验后发布；初版不引入 CRD、标签选择器、端口状态推断或 Kubernetes NetworkPolicy。

ACL 按 IPv4 源/目的和 protocol 匹配，不依赖 TCP/UDP 端口。因此合法分片虽在转发入口回退 Linux，在目标 egress 仍按 IP 头匹配同一拒绝规则；不能因非首片没有 L4 头而绕过。非法头和无可靠端点绑定单独处理。

规则对每个匹配报文生效，没有“旧连接继续授权”。对于 Service，最终检查看到的是该 hook 处 NAT 后的地址；发生 SNAT 时可能无法还原原始 Pod 身份。因此只对声明的 PodIP 直连范围给出 ACL 语义，不宣称 Service 原始身份隔离或 NetworkPolicy 合规。

非法 ACL 更新保留最后完整规则并报告 rejected；新增 ACL 不算已生效。紧急附件隔离独立使用 binding 门禁且读回验证。不能将日志报错当作内核拒绝已经生效。

## 7. 能力门禁与诊断

M0 先证明 clsact、FIB、redirect→目标 egress、CT kfunc/引用释放、checksum helper 可用。M2 完成本地快转；M3/M4 接入 Kubernetes 和完整 Service 验证；M6 验证 tunnel 设备行为。任何能力失败关闭对应快路径，慢路径保留，阶段结果仍标为未完成。

计数至少包含 redirect_requested、delivery_seen、slow_service、slow_ct_hit、slow_ct_error、slow_unknown_peer、slow_neigh、slow_mtu、slow_ttl、slow_options_fragment、slow_offload、slow_fib、drop_source、drop_endpoint、drop_acl、drop_parse、drop_mutation。统计单位为 hook invocation/skb，非物理帧数。

当前使用有限原因计数和控制面状态，不输出逐包事件。跨节点投递以接收端、TTL/MAC 抓包、目标 hook 计数共同证明。B3 校验和复用仅用于性能实验，未进入默认编译/镜像，见 [M7](experiments/M7.md)。
