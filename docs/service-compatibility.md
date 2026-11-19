# kube-proxy 与 eBPF 转发的边界（v5）

状态：限定配置的 Native 与 VXLAN Service 用例已验证，见 [M4](experiments/M4.md)、[M6](experiments/M6.md)。Service 归 kube-proxy 是用户确定的范围；本文件定义共存方式，不开发第二套 Service 负载均衡。下表是合同与候选矩阵，实际覆盖以实验记录为准。

## 1. 各自负责什么

- kube-proxy：观察 Service/EndpointSlice，生成服务后端选择与相关 NAT/过滤配置。
- Linux conntrack/NAT：保存和执行对应连接转换。
- knd-agent：配置 Pod 接入、路由、端点快照；读取明确的 ServiceCIDR 配置用于排除快路径，不编译 Service 后端表。
- BPF：满足条件才快转；遇到需要本机内核连接处理的流量，保留该内核路径。

首个验收环境固定 kube-proxy **iptables 模式**，同时记录 iptables 的 legacy/nft 后端。iptables-nft 不等于 kube-proxy 原生 nftables 模式。原生 nftables 共存可作为后续独立兼容实验，不同时维护多套未验证配置。IPVS 不作为首版基线。[Kubernetes 服务代理](https://kubernetes.io/docs/reference/networking/virtual-ips/)

## 2. 仅排除 VIP 为什么不够

例如 A:40000→VIP:80 被转换为 A:40000→B:8080；B 的回包是 B:8080→A:40000。回包目的地址已经是 PodIP，只按目的 IP 判定快路径会跳过持有原始 NAT 状态的节点上的反向转换。

因此，以下两条必须同时满足：

1. VIP/NodeIP/非普通 Pod 目的不进入快路径；ServiceCIDR 还要与 PodCIDR 明确不重叠。
2. 对拟快转的 IPv4 TCP/UDP 报文，在每个入口检查本机 conntrack；命中即继续本机 Linux 路径。

该检查必须覆盖 host-veth ingress、underlay ingress 和后续 kndvx0 ingress。只在客户端 veth 检查是不完整的。

## 3. 候选实现：只查询内核 conntrack

选定的候选使用 `bpf_skb_ct_lookup` kfunc，在当前 host network namespace、默认 zone 中，按报文实际 TCP/UDP 五元组查询。调用前初始化完整 opts 并清零 error；成功取得引用后必须 `bpf_ct_release`，不修改、创建或删除 conntrack 条目。

| 查询结果 | 处理 |
|---|---|
| 找到条目 | 释放引用，原样 SLOW；不必读取 NAT 状态位，普通已跟踪流也保守回内核 |
| 明确返回未找到（-ENOENT） | 才可继续其他快路径条件检查 |
| 其他错误 | 原样 SLOW，并记录 ct_lookup_error |
| kfunc 无法加载/语义未验证 | 节点 fastReady=false，全部保留 Linux 转发；不以“查询失败”当作“没有 NAT” |

Linux v6.6 的实现展示了该查询可匹配 conntrack 哈希中的方向，并返回引用和错误原因；它是 **kfunc，接口并非稳定 ABI**。必须用所选内核实际编译、加载和 NAT 报文测试，不能仅依据版本号启用。[内核 conntrack BPF 实现](https://raw.githubusercontent.com/torvalds/linux/v6.6/net/netfilter/nf_conntrack_bpf.c)、[kfunc 稳定性](https://docs.kernel.org/bpf/kfuncs.html)

这是“旁路条件检查”，不是前版自研 TCP 授权状态机。首版不维护 BPF flow_state、不用 BPF timer，也不复制 kube-proxy 的连接/NAT 表。

## 4. 请求、回包与中间节点

请求在源节点先因 VIP 分类进入 Linux；正常情况下，conntrack/NAT 状态在请求送往后端前已经建立。回包抵达 NAT 所在节点时，按回包五元组查到关联状态并回内核，完成反向转换。

其他节点收到已转换的 PodIP 报文时可以没有本机 NAT 状态；该段在满足其他条件时仍可快转。因此不声称所有 Service 报文在所有节点都只能走慢路径，验收目标是**需要转换的节点必须保留转换行为**。

同节点后端、跨节点后端、客户端自己成为后端的 hairpin 三种路径分别验证。NodePort 的原始目的为节点地址，因此原样交内核；外部来源/目标不在已确认 Pod 集合内，也不进入快路径。

CoreDNS Service 需验证 UDP 53 和 TCP 53。服务端点变化由 kube-proxy 接管；项目不为它选择新后端，不清理其旧连接状态。

## 5. 协议与运行边界

- 快路径仅处理可完整解析的 IPv4 TCP/UDP；分片、IPv4 options、ICMP、初版 GSO/GRO 大 skb 等走 Linux，避免在无法得到完整五元组时错误跳过 NAT。
- 只承诺固定的默认 zone、受控 NAT 配置；自定义 conntrack zone、NOTRACK、任意第三方 NAT、透明代理和 Service Mesh 不在首版范围。
- 正常 Linux conntrack 的超时、表满、清表及内核重启会影响 Service；项目不额外承诺这些情况下的连接保留。不能用负缓存将一次“查无记录”长期当作允许快转的依据。
- 快路径可能绕过普通 Netfilter FORWARD 等规则，不能宣称与任意宿主防火墙等价。项目自己的端点最终检查必须在两条路径上都执行。
- 升级、参数变化或能力检测失败时切回 fastReady=false；慢路径本身必须已经验收。没有慢路径基线就不允许声称安全降级。

## 6. 启用快路径前的验收

| ID | 用例 | 证据 |
|---|---|---|
| SVC-01 | ClusterIP，本地/远端 TCP 后端 | 正常握手与长流；客户端收到的对端仍是 VIP；NAT 节点 ct_slow 计数命中 |
| SVC-02 | UDP Service 与 DNS 的 UDP/TCP | 请求/回包正确，不能只以 TCP 成功推断 DNS 可用 |
| SVC-03 | 同节点 hairpin | 源/目的/NAT 报文符合 Linux 基线，无绕过返回转换 |
| SVC-04 | NodePort，externalTrafficPolicy=Cluster | 外部请求正常，记录实际源地址语义；Local/DSR 等不扩张承诺 |
| SVC-05 | 调整后端、连接持续、同一 Pod 同时使用直连和 Service | 按各自连接路径运行，VIP 连接不被错误切入快路径 |
| SVC-06 | 强制 ct 查询错误、缺少 kfunc、缓存未就绪 | 不进行 redirect，已有 Linux 网络仍可工作 |
| SVC-07 | ct miss 与 hit 的方向测试 | 真实 SNAT/DNAT 的原始/回包 tuple 可识别；每次引用均释放 |
| SVC-08 | Native/VXLAN 下重复上述用例 | NAT 所在节点的处理不因网络模式改变而跳过 |

保存快路径开/关对照、节点抓包、conntrack 只读记录、BPF 原因计数和接收端输出。任何关键回包测试未通过，M4 不得交付 Service 共存版；保持基线模式继续修复。
