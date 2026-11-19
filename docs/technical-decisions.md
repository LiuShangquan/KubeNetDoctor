# 技术决策与取舍（v5）

更新日期：2026-11-17。v5 根据用户要求改为 eBPF 转发主线，Service 交给 kube-proxy；实际功能实验见 [实施状态](implementation-status.md)。[v4 完整快照](archive/v4-policy/设计方案.md) 保留历史，不能混用旧阶段要求。

## 1. 决策表

| 编号 | 决定 | 原因与代价 | 验证点 |
|---|---|---|---|
| D01 | 自研 Go CNI + 节点 Agent + TC eBPF 数据面 | 直接覆盖网络接口生命周期、控制面转换和逐包转发开发；不把部署现成 CNI 当主要成果 | runtime 调用与真实 BPF 转发 |
| D02 | 每 Pod /32、routed veth，不使用 bridge | 同节点、Native 和 VXLAN 共用三层语义；新增网关/静态邻居与路由管理责任 | M0 网关、本机源地址、TTL 与双向路径 |
| D03 | Linux 路由为基线和慢路径；BPF 复用 FIB/邻居 | 自己实现资格判断、头部处理和输出，保留成熟异常路径；不重复完整路由栈 | FIB 结果核验、异常回退保持原包 |
| D04 | TC clsact + bpf_redirect(flags=0)；目标 veth egress 最终检查 | 避免首次引入多种 hook/redirect 语义；仍需证明目标检查实际执行 | M0 egress DROP 实验，后续所有路径回归 |
| D05 | Service 后端选择与 NAT 归 kube-proxy/Linux | 控制项目规模；加速仍必须尊重反向 NAT | [Service 合同](service-compatibility.md) 的完整门禁 |
| D06 | 每个快转入口只读查询本机 CT；hit/error 均 SLOW | VIP 排除不足以保护回包；保守处理所有已有 CT，不依赖内部 NAT 状态位 | kfunc 加载、引用释放、原始/回包方向、错误回退 |
| D07 | 首次固定 kube-proxy iptables 模式 | 减少兼容矩阵；记录 legacy/nft 后端，原生 nftables 模式另验 | 实际集群配置和 ClusterIP/DNS/NodePort |
| D08 | host-local IPAM，NodeCIDR 复用 Kubernetes 分配 | 把开发投入放在附件事务与转发，不先实现分布式 IPAM | CIDR 不重叠、并发分配/释放、租约恢复 |
| D09 | Node/Pod→有界不可变快照，单节点串行发布 | 学习控制面到数据面转换，避免半张转发表；不承诺跨节点事务 | 旧任务覆盖、容量、故障与内存峰值 |
| D10 | 逐包、无状态 Pod 对拒绝 ACL 作为辅助功能 | 保留转发后的控制能力，减小 NetworkPolicy/连接状态范围 | 快慢路径同一目标门禁；只承诺直连 PodIP 语义 |
| D11 | Native 优先，Linux VXLAN 第二模式 | 先理解普通三层路径，再理解封装；自研 VXLAN 控制，不重复封装协议 | 路由/邻居/FDB、MTU、内外层与 CT 验证 |
| D12 | 同拓扑、完整检查、真实快路径比例的性能对照 | 不能以遗漏检查获得的速度冒称等价优化 | B0/B1/B2/B3、重复测量、剖析和原始数据 |
| D13 | 不采用 B3 checksum 复用优化 | 五轮剖析与短时吞吐/RTT 未显示稳定收益；默认保留目标独立检查 | [M7 结果与边界](experiments/M7.md) |

## 2. CNI、VXLAN、eBPF 与 Service 不是同一层的选项

| 名称 | 解决的问题 | 本项目关系 |
|---|---|---|
| CNI 规范/插件 | runtime 如何为 sandbox 配置、检查、删除网络附件 | 自研 knd-cni，按选定规范版本实现 |
| Calico / Cilium | 实现 CNI 接入、节点控制与数据面的完整网络系统 | 阅读和对照；专用开发集群不由其同时接管普通 Pod 网络 |
| Native routing | Pod 流量不做 overlay 封装，依赖底层可路由 | 第一种网络模式 |
| VXLAN | 用外层 UDP/IP 在节点间承载内层以太帧 | Linux 负责封解装，Agent 管配置，BPF 可参与隧道前后转发 |
| eBPF | 在规定内核 hook 执行程序与访问 Maps | 自研 TC 快路径与目标检查 |
| kube-proxy | 观察 Service/EndpointSlice 并配置 Service 数据面 | 复用，项目不重复维护后端选择 |
| iptables / nftables | 配置 Netfilter 过滤和 NAT 等行为 | 首版 kube-proxy 使用 iptables 模式；它可能通过 iptables-nft 后端运行 |
| IPVS | Linux 内核四层负载均衡机制 | 本项目不采用其 kube-proxy 模式 |

VXLAN 可以与 eBPF 同时存在；“Calico=VXLAN、Cilium=eBPF”的一对一划分不成立。Calico 有不同数据面和路由方式；Cilium 也支持 native routing 和隧道。项目的选择是 **自研接入与控制，TC eBPF 主动转发，kube-proxy 提供 Service**。[Calico 数据路径](https://docs.tigera.io/calico/latest/reference/architecture/data-path)、[Cilium 路由](https://docs.cilium.io/en/stable/network/concepts/routing/)、[Kubernetes 服务代理](https://kubernetes.io/docs/reference/networking/virtual-ips/)

iptables 是用户空间配置工具及其规则接口；实际包处理在内核。iptables-nft 使用 nftables 基础设施，不能据此把 kube-proxy 的模式写成原生 nftables。[Netfilter nftables 项目](https://www.netfilter.org/projects/nftables/index.html)

## 3. v4 中哪些工作退出必做范围

- bridge 接入改为逐 Pod 三层路由；不先实现旧拓扑再迁移。
- NetworkPolicy selector/并集编译、完整入站 TCP 策略改为后续扩展。
- 自研 TCP 授权状态机、flow_state、spinlock/BPF timer 回收退出阶段主线。
- BPF 从以 PASS/DROP 为中心改为 FIB→TTL/checksum/MAC→redirect，DROP 只承担入口和目标门禁。
- Service 从“不是自研功能”补充为“必须验证的兼容边界”，由 kube-proxy 实现不代表可以忽略其数据路径。
- 独立诊断平台、集群运维平台、TCP 客户端/服务端练习均不加入。

## 4. 实际冻结参数与未验收预算

| 项目 | 实际实现/环境 | 证据或边界 |
|---|---|---|
| 环境 | WSL 6.18.33.2；两个独立 VM Linux 7.0.0-43-generic | 真实 CT/FIB/redirect，保留一次 guest panic 的未解决根因 |
| 软件 | Go 1.26.8 / Clang 21.1.8 / K3s 1.37.2+k3s1 / containerd 2.3.7-k3s1；CNI 行为 1.0 | 库版本固定在 go.mod/go.sum，详见 environment.md |
| 地址 | Node 192.168.77.11/12；PodCIDR 10.244.0.0/24 和 10.244.1.0/24；Service 10.96.0.0/12；GW 169.254.1.1；VTEP 各 PodCIDR 基址 | runtime 与实际报文验证；正文其他地址只作示例 |
| CT | 当前 host netns、默认 zone；只有 -ENOENT 才作为 miss | hit/error 回内核；强制缺 kfunc 的基线降级有实验 |
| 容量/预算 | 端点合计 256、路径 32、ACL 256、快照 4096 为编译上限 | 64 本地 Pod、Agent 256 MiB、Map 峰值 64 MiB 只是原候选预算，未做压力认证 |
| MTU/offload | Native 1500，VXLAN 1450/VNI42/UDP4789；大 skb SLOW | M6 MTU 边界与抓包；完整 offload 开关矩阵未完成 |

失败时关闭相应快路径并保留已经通过的基线，同时把相关阶段记为未完成。若需要替换 CT guard 或 redirect 方案，先更新本决策及 Service/目标检查合同，再进入实现；不在测试失败后悄悄删掉语义要求。

## 5. 直接参考

- [CNI 1.0 规范](https://github.com/containernetworking/cni/blob/spec-v1.0.0/SPEC.md) 与 [host-local](https://www.cni.dev/plugins/current/ipam/host-local/)：接口和分配器边界。
- [Linux v6.6 BPF UAPI](https://raw.githubusercontent.com/torvalds/linux/v6.6/include/uapi/linux/bpf.h)：FIB/redirect helper 合同；这是阅读样本，不是已锁定运行版本。
- [conntrack BPF 实现](https://raw.githubusercontent.com/torvalds/linux/v6.6/net/netfilter/nf_conntrack_bpf.c) 与 [kfunc 稳定性](https://docs.kernel.org/bpf/kfuncs.html)：只读查询的候选实现及兼容风险。
- [Linux VXLAN](https://docs.kernel.org/networking/vxlan.html)：隧道设备、FDB 与内核复用。

源码阅读最终记录实际使用 commit，文档中的版本示例不能替代实验环境记录。
