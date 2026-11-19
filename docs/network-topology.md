# 网络拓扑与数据包路径（v5）

日期：2026-11-17。Native 和 VXLAN 已在两个独立内核 VM 验证，最终部署为 VXLAN。主线为 TC eBPF 三层转发；Linux 路由是功能基线与慢路径，Service 归 kube-proxy。本文保留下面的逻辑示例地址以便讲解；实际集群节点为 192.168.77.11/12，PodCIDR 为 10.244.0.0/24 与 10.244.1.0/24，ServiceCIDR 为 10.96.0.0/12，节点网卡名 underlay0。Pod IP 以 kubectl 查询为准。程序判定细节见 [数据面设计](datapath-and-policy.md)，抓包证据见 [M4](experiments/M4.md)、[M6](experiments/M6.md)。

## 1. 统一采用三层 veth 拓扑

每个普通 Pod 一对 veth，宿主端不加入 bridge。Pod 使用 /32 地址；宿主为每个本地 Pod 安装 /32 路由。前期与最终版本使用同一拓扑，只改变快路径开关，不先开发 bridge 后再整体迁移。

| 对象 | Node A | Node B |
|---|---|---|
| 节点 underlay IP 示例 | 192.168.56.11 | 192.168.56.12 |
| Kubernetes 分配的 PodCIDR 示例 | 10.244.1.0/24 | 10.244.2.0/24 |
| 示例 Pod 与接口地址 | Pod A：10.244.1.2/32；Pod C：10.244.1.3/32 | Pod B：10.244.2.2/32 |
| Pod 下一跳示例 | 169.254.1.1 | 169.254.1.1 |
| 后期 VXLAN VTEP 地址 | 10.244.1.0/32 | 10.244.2.0/32 |

下文地址均为讲解示例，不是可直接执行的当前集群参数。节点的 /24 是分配池，Pod 接口的 /32 是接入模型，二者用途不同。

在 Pod A 内配置的逻辑意图：

```text
eth0: 10.244.1.2/32
169.254.1.1/32 dev eth0 scope link
default via 169.254.1.1 dev eth0
permanent neighbor: 169.254.1.1 → 本 Pod 的宿主 veth MAC
```

节点用自有 dummy 设备 `knd-gw` 持有该网关 /32；每个 Pod 都用自己对应宿主 veth 的 MAC 作为下一跳 MAC。CNI 配置静态邻居，首版不自研 ARP 代理。需实测 dummy、本地地址、源地址选择及宿主 ARP/sysctl 行为。

节点配置 `PodIP/32 dev host-veth` 路由和 `PodIP → Pod MAC` 永久邻居；必要时为该路由明确使用 NodeIP 作为本机发起流量的源地址。自己的 PodCIDR 配置受管 blackhole 聚合路由，由更具体的本地 /32 覆盖，避免已删除 Pod 的地址落入默认路由形成环路。

host-local 只提供租约地址；CNI 规范化为 /32 和上述路由，不能机械沿用 IPAM 输出的网关。地址池预留网段基址、.1 和广播地址，后续 VTEP 的 /32 不分配给 Pod。

## 2. Hook 合同

| 位置 | 程序职责 |
|---|---|
| host-veth TC ingress | 本地源地址/附件检查；判断快慢路径；合格报文进行三层转发 |
| underlay NIC TC ingress | 仅识别允许的远端 Pod→本地 Pod 流量；其他报文保留内核路径 |
| kndvx0 TC ingress，M6 | 对内核已解封装的内层报文做本地投递 |
| 每个 host-veth TC egress | 最终端点 Ready/目的地址检查；M5 的实验 ACL；不再次重定向 |

统一选择 `bpf_redirect(outIfIndex, 0)` 的目标设备 egress 路径；不使用 `bpf_redirect_peer`。源端程序只在 ingress 转发。**M0 必须证明重定向和普通内核路由都经过目标 veth egress；故意在该 hook 丢弃时，接收端应完全收不到对应流量。** 不把文档中的路径当作测试结果。

## 3. 同节点：Linux 基线与 eBPF 路径

因为 Pod 使用 /32，Pod A→Pod C 也交给节点网关，固定为三层语义。

```text
Linux 基线：A eth0 → host-veth-A ingress（继续内核）
 → Linux IPv4/Netfilter/路由 → host-veth-C egress 最终检查 → C eth0

eBPF：A eth0 → host-veth-A ingress
 → 检查端点、快路径条件及本机 conntrack
 → bpf_fib_lookup，结果必须指向已登记的 host-veth-C
 → TTL 减 1、更新 IPv4 头校验和、改写下一跳 MAC
 → bpf_redirect → host-veth-C egress 最终检查 → C eth0
```

IP 源/目的与 TCP/UDP 端口保持不变。eBPF 实现的是使用内核 FIB/邻居能力的三层快路径，不是重新实现全部路由协议。基线和快路径应具有相同的三层跳数。

## 4. 跨节点 Native 路径

Agent 在 A 上安装 `10.244.2.0/24 via 192.168.56.12 dev <underlay-if>`，B 有反向路由。两节点需直接下一跳可达，underlay 允许 Pod 源地址，IPv4 forwarding、过滤和 rp_filter 配置经过验证；云上“同一子网”不是充分条件。

```text
Pod A → host-veth-A ingress 的 eBPF
 → 端点/节点信息与 FIB 结果核对
 → TTL/MAC/校验和处理 → 重定向 Node A underlay 网卡
 → underlay
 → Node B underlay ingress 的 eBPF
 → conntrack 检查与本地目标核对 → FIB/报文处理
 → 重定向 host-veth-B egress 最终检查 → Pod B
```

每个承担三层转发的节点各减少一次内层 TTL。任一节点可因其本机条件进入 Linux 路径；进入慢路径前必须保持报文未被快路径修改，避免二次扣 TTL。Pod→Pod 不做项目自有 SNAT。

在物理入口只允许已登记远端 Pod、正确来源节点/输入设备类别及本地目标参与快转；不把默认路由、节点管理流量、任意外部报文变成快路径。IP→节点关系依赖可信实验 underlay，不提供加密身份认证。

## 5. Service：保留需要执行 NAT 的内核路径

```text
Pod A → ClusterIP
 → 源节点 veth ingress：ServiceCIDR 命中，原样交内核
 → kube-proxy 配置的规则选择后端并 DNAT，Linux conntrack 建立状态
 → 普通路由到本地或远端 Pod B → 目标 veth egress → B

Pod B 回包 → 到达持有 NAT 状态的节点
 → ingress BPF 查到本机 conntrack：原样交内核
 → 内核执行反向 NAT → 客户端 veth egress → Pod A
```

远端节点收到已 DNAT 的 PodIP 报文时，若它本机没有相关 conntrack 且满足其他条件，该网络段可以快转；关键保证是 **NAT 所在节点不会因快转跳过其 NAT 处理**。具体合同、失效条件和测试见 [Service 兼容设计](service-compatibility.md)。

## 6. VXLAN：后期第二种跨节点模式

复用 Linux VXLAN 封装/解封装。候选设备 `kndvx0`，IPv4 outer、VNI 42、显式 UDP 4789、nolearning、静态 peer，不加入任何 bridge。[Linux VXLAN](https://docs.kernel.org/networking/vxlan.html)

Agent 自研设备和三类对象控制：远端 PodCIDR 经远端 VTEP /32 的 onlink 路由；VTEP IP→VTEP MAC 邻居；VTEP MAC→NodeIP 的 VXLAN FDB。MAC 按 Node UID 固定生成并检测冲突。创建先设备/邻居/FDB、最后路由；删除先撤路由，按所有权清理其余对象。

```text
A veth ingress eBPF → FIB 输出 kndvx0
 → 内层 TTL/MAC/校验和处理 → redirect 到 kndvx0
 → Linux 封装外层 NodeA IP / NodeB IP + UDP + VXLAN
 → 对端网卡：外层为节点流量，继续 Linux
 → Linux 解封装 → kndvx0 ingress eBPF
 → 本地投递及目标 veth egress 最终检查 → Pod B
```

M6 先证明全部走 Linux 的隧道路径，再验证 BPF 重定向到隧道设备及解封装后的 hook。若该组合不满足 verifier/helper/设备语义，VXLAN 保留明确的慢路径并报告该阶段未达到快转目标，不换名宣称已完成。

IPv4 outer、无额外封装时，underlay MTU 1500 对应候选 Pod MTU 1450；实际路径重新测量。模式每次实验固定，切换时重建附件，不做在线切换和自动混合选路。[封装与 MTU](https://docs.cilium.io/en/stable/network/concepts/routing/)

## 7. 环境与所有权

首个平台 Linux amd64；候选 6.6/6.8 系列，必须固定实际补丁版本、内核配置/BTF、NF_CONNTRACK 和 kfunc 能力。Kubernetes/containerd、Go/Clang、CNI/netlink/ebpf 库与工具版本均由 M0 实测锁定，不宣称整个内核系列兼容。

单 underlay 网卡、单 IPv4 PodCIDR/节点、明确的 ServiceCIDR、默认 conntrack zone、无第三方 NOTRACK/自定义 NAT、无其他主 CNI。需要修改宿主转发/过滤时，仅配置隔离实验环境中明确属于项目的规则，不清空整套防火墙或覆盖 kube-proxy 规则。

正式实验推荐独立内核的控制平面加两工作节点；kind 只用于部分集成，不代替独立多节点性能。当前宿主可用性仍待确认，没有部署结果。
