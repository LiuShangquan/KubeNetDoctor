# 网络拓扑与环境设计

状态：设计候选，尚未部署。本文拥有网络模式、地址规划、路由与隧道规则；策略语义见 [datapath-and-policy.md](datapath-and-policy.md)。

## 1. 实验拓扑

前期一个 Linux 环境内创建 netns/veth 验证 TC 与 CNI；正式跨节点采用独立内核的虚拟机。推荐一台控制平面、两台工作节点；资源不足可先让控制平面承担一个实验端点，但须注明资源干扰。

| 节点 | underlay 示例 | PodCIDR 示例 | bridge 网关 | VXLAN 本地 /32 |
|---|---|---|---|---|
| worker-a | 192.168.56.11 | 10.244.1.0/24 | 10.244.1.1 | 10.244.1.0/32 |
| worker-b | 192.168.56.12 | 10.244.2.0/24 | 10.244.2.1 | 10.244.2.0/32 |

以上是示意，不是可直接部署的已核实地址。M0 检查与宿主、VPN、ServiceCIDR、现有路由的冲突后确定。Pod 地址池排除网段基址、网关和广播地址。VXLAN 的 /32 使用网段基址作为独立主机地址，不把它分配给 Pod。

实验 Namespace 默认为 `knd-lab`，其名称通过配置固定；不根据容易变化的标签隐式扩大管理范围。系统 Namespace 不启用本项目的策略过滤。部署使用专用实验集群，不替换已有业务集群的主 CNI。

## 2. Native 模式

```text
同节点：Pod A → host veth ingress → bridge → host veth egress → Pod B
跨节点：Pod A → host veth ingress → bridge 网关/IP 路由
        → underlay → 对端 IP 路由 → bridge → host veth egress → Pod B
```

示例 A 上的远端路由意图：`10.244.2.0/24 via 192.168.56.12 dev <underlay-if>`；B 有反向路由。Linux 负责实际 IP 转发，项目不对 Pod→Pod 流量做 SNAT。

前提是节点下一跳直接可达、underlay 允许带 Pod 源地址的流量、双向回程存在。云上“同一子网”不等于满足这些条件。IPv4 forwarding、宿主过滤、rp_filter、bridge netfilter、MTU 要以实验验证，禁止清空整个防火墙解决问题。

节点路由使用自定义 protocol 标记、精确属性与所有权记录限制变更范围；遇到外部同目的路由冲突应失败，不覆盖。节点暂时 NotReady/API 断连不解释为节点已删除。

## 3. VXLAN 模式

第二种模式复用内核 VXLAN，不自行在 BPF 中重写封装报文。采用 routed VXLAN：`kndvx0` 不加入本地 Pod bridge；每节点仍有独立 PodCIDR，节点间通过 VTEP 传输内层报文。

候选固定参数：IPv4 outer、单 VNI 42、UDP 4789、`nolearning`、静态邻居/FDB、单 underlay 网卡。Linux VXLAN 的 UDP 端口必须显式指定，不能依赖与其他实现相同的默认值。[Linux VXLAN 文档](https://docs.kernel.org/networking/vxlan.html)

在 A 上为 B 安装三类对象：

| 对象 | 计划 |
|---|---|
| 路由 | B PodCIDR 经 B 的 VTEP /32、出接口 kndvx0，明确 onlink |
| 邻居 | B VTEP /32 → B VTEP MAC，永久邻居 |
| FDB | B VTEP MAC → B underlay IPv4，在 kndvx0 上的静态隧道条目 |

VTEP MAC 由 Node UID 按固定算法生成 locally administered unicast 地址；构建 peer 集合时检测冲突，不能假定截断哈希永不冲突。各节点依据同一 Node 快照计算对端 MAC，无需额外把全部端点写进 Node annotation。

创建顺序：本地设备和地址就绪 → 对端邻居 → FDB → 路由；删除先撤路由再删除不再引用的 FDB/邻居。多对象 netlink 更新不是原子事务，失败需要回滚本次已创建的对象并报告；不能声称更新期间零丢包。

参考 Flannel 的路由、邻居与 FDB 配合方式，学习后独立实现本项目受限模型；固定源码 commit 后记录差异，不能将复用内核 VXLAN 描述为自研封装协议。[Flannel VXLAN 控制路径](https://github.com/flannel-io/flannel/blob/master/pkg/backend/vxlan/vxlan_network.go)

```text
Pod A 内层 TCP/IP
 → 本地 bridge 网关
 → kndvx0 按路由/邻居/FDB 选择对端
 → 外层 NodeA IP / NodeB IP + UDP + VXLAN + 内层 Ethernet/IP/TCP
 → Node B 解封装
 → 本机路由/bridge
 → 目标 host veth egress 执行同一套 TCP 策略
```

策略 hook 位于端点 veth，因此匹配的是内层 Pod IP，不把 VTEP 地址误当业务来源。

## 4. MTU 与路径对照

IPv4 outer、无额外封装条件下，VXLAN 相对 underlay L3 MTU 需预留外层 IPv4/UDP/VXLAN 与内层 Ethernet 共 50 字节；underlay MTU 1500 时，候选 Pod MTU 为 1450。额外隧道、VLAN/路径 MTU 条件需重新确认，不能机械固定 1450。[Cilium 封装与原生路由说明](https://docs.cilium.io/en/stable/network/concepts/routing/)

实验分开回答两个问题：

1. 相同 Pod MTU、相同负载下，native 与 VXLAN 路径的 CPU/吞吐/延迟有何差异。
2. 各模式使用其合适 MTU 时，实际可达到的行为有何差异。

记录 offload、GSO/GRO、分片与 ICMP 路径；不能把抓包中的超大 skb 直接当成超 MTU 的线上帧。策略程序的分片限制与 MTU 故障要分开归因。

首版每次运行选择一个全局模式，不做在线模式切换或自动故障切换。两种模式的测试使用重建的实验附件/集群。自动混合选路、跨可用区隧道优化不属于 R2 完成条件；JD 的混合拓扑要求只能部分覆盖。

## 5. 环境与版本决策

建议候选为支持所选 TC、BPF map-in-map、BTF 的 Linux 6.6/6.8 系列环境，具体发行版与补丁版本以 M0 实机验证为准；不把该候选当作已支持的内核范围。首个 ABI 目标固定 Linux amd64/little-endian。

| 项目 | M0 应记录的内容 |
|---|---|
| Linux/虚拟化 | 发行版、内核完整版本、架构、CPU/RAM、独立内核与否 |
| 网络 | 网卡、实际 underlay/Pod/Service 网段、MTU、转发与过滤配置 |
| Kubernetes/runtime | Kubernetes/containerd 完整版本、CNI 参数、节点网段分配方式 |
| 工具链 | Go、Clang/LLVM、libbpf headers、cilium/ebpf、CNI/netlink/netns 库的固定版本 |
| BPF 能力 | TC clsact direct-action、Map 类型、锁/时间/helper、bpffs 与 verifier 实测 |
| 测试工具 | iproute2、bpftool、tcpdump、iperf3、socat/nc 的实际版本与可用功能 |

CNI 规范固定从 1.0.0 子集实现开始；规范版本与 CNI 库版本分别记录。TC 固定先用 clsact/direct-action，若 M0 证据要求改为 TCX，更新 ADR 后再实现，不同时维护两套后端。

本机此前只确认可找到 Docker/WSL 等命令，Docker Linux Engine 未连通，WSL 查询返回 E_ACCESSDENIED。尚未确认内核、BTF、权限、机器容量。先验证，不购买云主机或改动系统配置来假定环境可用。

kind 可做部分 API/部署验证；其容器节点共享内核，不能充当独立节点故障隔离或真实网络性能证据。正式性能报告注明 VM 共享宿主 CPU 的干扰；独立内核也不等于独立物理硬件。
