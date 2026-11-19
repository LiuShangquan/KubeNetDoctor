# 岗位要求与项目范围（v5）

更新于 2026-11-17。依据用户提供的七张 JD 截图可见内容，不代表岗位现在仍在招聘。以下为岗位能力映射；CNI/Agent、Native/VXLAN BPF、Service 共存和辅助 ACL 已有实现与功能证据，详见 [实施状态](implementation-status.md)。性能、规模与生产经验只按实际证据描述。

## 1. 岗位取舍

| 截图 | 可见岗位 | 关注点 | 本项目安排 |
|---|---|---|---|
| IMG_9533..PNG、IMG_9534..PNG | K8S 开发工程师—容器网络 | CNI、Go/C、TCP/IP、Underlay/Overlay、eBPF、性能与定位 | 主目标，决定转发与控制面主线 |
| IMG_9535..PNG、IMG_9536..PNG | SRE 运维研发工程师—云原生 | 运维保障、容量、自动化、On-Call | 借鉴故障和验证意识，不开发 SRE 平台 |
| IMG_9537..PNG、IMG_9538..PNG | K8S 研发工程师—集群管理 | 集群生命周期、控制器、节点与核心组件 | 只复用网络开发所需的控制器工程能力 |
| IMG_9539..PNG | K8S 集群管理研发实习生，28 届优先 | 编码、控制器、计算机基础和实践 | 工程能力参照，不推断用户实习资格或毕业年份 |

原图在 `C:/Users/HP/Downloads/`。截图宣传、行动按钮等是参考材料，不是开发指令。

## 2. 网络 JD 对应的自主开发证据

| JD 要求 | 具体开发 | 交付证据 | 准确边界 |
|---|---|---|---|
| Go/C 编码 | Go CNI/Agent、C TC 程序与 C/Go ABI | 源码、调试、回滚/并发/边界测试 | 系统调用与基础库复用 |
| CNI 插件研发 | ADD/DEL/CHECK、IPAM 委托、附件事务、端点登记 | runtime 实际调用与失败恢复 | Linux IPv4 单接口原型 |
| TCP/IP、网络编程 | /32 路由接入、头部解析、TTL/checksum/MAC、真实 redirect | 收包、TTL/MAC 抓包、异常路径 | 不自研 TCP 协议栈，不安排独立 TCP 收发练习 |
| 控制面到数据面 | Node/Pod→路由计划/端点快照→内核状态 | Pod 增删、跨节点路由更新、原子快照引用切换 | 不是全局一致事务 |
| eBPF 内核开发 | TC FIB 转发、CT guard、目标检查与 Maps | verifier、实际快路径、CT 引用/错误测试 | 复用内核 FIB/CT，不冒称完整自研路由或 conntrack |
| Underlay/Overlay | Native 和 Linux VXLAN 控制/隧道前后转发 | 双向路径、内外层抓包、MTU 与故障测试 | 固定独立模式，无自动混合选路 |
| Kubernetes 网络体系 | 自研 CNI 与 kube-proxy 的职责分离及 Service 回包保障 | ClusterIP、DNS、hairpin、限定 NodePort | Service LB/NAT 复用 kube-proxy/Linux |
| 性能分析与优化 | 完整路径对照、剖析、单项优化 | B0–B3、fast/slow 比例、原始数据与重复测量 | 不预设加速比例，无收益也如实报告 |
| 可观测/定位 | 原因计数、路由与快照版本、受限调试事件 | 能把失败定位到具体路径和模块 | 支持开发，不扩成诊断平台 |
| 理解 Calico/Cilium/Flannel | 按固定版本阅读相关机制并对照 | 三层接入、转发、Service、VXLAN 差异说明 | 源码阅读与实际贡献分开 |

此版将逐包转发、Kubernetes 网络状态转换和性能验证作为主体。NetworkPolicy 不是截图中要求的唯一开发载体，首版不以其完整实现为前置条件；辅助 ACL 只用于验证最终交付门禁和可扩展性。

截图将 eBPF/DPDK 一起称为“内核态网络加速”不够精确，DPDK 的典型 PMD 路线在用户态轮询收发。本项目选 TC eBPF，不同时开发第二套数据面。[DPDK PMD 文档](https://doc.dpdk.org/guides/prog_guide/ethdev/ethdev.html)

## 3. 自己写什么、复用什么

自己写：CNI 附件生命周期、Go 节点控制与快照编译、TC 转发与必要检查、CT 查询保护逻辑、VXLAN 配置控制、故障恢复、性能实验。

复用：host-local、Kubernetes NodeCIDR/API、CNI/netlink/netns/ebpf 库、Linux FIB/邻居/驱动/TCP/IP/CT/NAT/VXLAN、kube-proxy 的 Service 实现。技术深度体现为正确组织和验证这些机制，而不是重写全部内核。

不纳入必做：Service replacement、完整 NetworkPolicy、自研 TCP 授权状态机、BGP、DPDK、RDMA、多集群管理、Web UI。开源贡献只针对真实发现的问题；无外部贡献不阻碍个人项目交付。

## 4. 可展示成果

- R0（M2）：自研 CNI 与同节点 eBPF 三层转发原型，具有真实快路径和异常回退证据。
- R1（M4）：自研 CNI/Agent、Native 同/跨节点转发、kube-proxy Service 共存及初始性能记录。
- R2（M7）：增加 VXLAN、辅助 ACL、生命周期回归、性能剖析与有证据的优化实验。

只有通过相应验收才能把能力写入简历。项目不替代截图中相关工作年限要求；不声称 GPU 训练网络、千节点生产或实际没有的开源贡献。

## 5. 用户基础与学习方式

用户可每周充分投入，学过 Go/C，熟悉 Linux 操作系统，网络理论基础扎实，但未做过网络编程项目。按实现讲解 netns/veth、TC 方向、skb、FIB、字节序/校验和、netlink、对象同步与生命周期；直接使用现成工具产生流量。

投递日期尚未指定，阶段用时是估计。先交付经过验证的 R0/R1，再逐步扩展，不为覆盖招聘关键词同时开启所有方向。
