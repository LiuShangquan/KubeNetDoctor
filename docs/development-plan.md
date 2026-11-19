# 开发阶段与自主实现清单（v5）

状态：保留的开发/学习阶段计划；实现与实际测试结果以 [实施状态](implementation-status.md) 为准。用户已要求自主完成后逐步学习，因此没有逐阶段等待确认。用户可充分投入，学过 Go/C，熟悉 Linux 和网络理论；不安排独立 TCP 收发实验，现成工具产生流量。主线为转发，Service 复用 kube-proxy。

按每周约 5 个有效开发日估算，共 41–62 个开发日，约 9–13 周；是候选工作量，不是承诺。M0 后依据环境与调试速度重估。阶段编号与 v4 已重排，旧 M4 的 TCP 状态机不再是当前要求。

## 1. 每阶段的开发与验收

| 阶段 | 候选用时 | 必须自己实现的内容 | 复用的能力 | 交付与验收 |
|---|---|---|---|---|
| M0 机制与环境验证 | 3–5 日 | 最小 TC 解析/redirect/checksum 探针、CT 查询能力探针、hook 计数 | netns/veth、iproute2、已有流量工具、内核 FIB/CT | 三层 /32 拓扑可用；redirect 确实经过目标 egress；CT hit/miss/error 与 NAT 回包可区分；保存版本与 verifier 证据 |
| M1 最小 CNI 与 Linux 基线 | 5–7 日 | CNI ADD/DEL/CHECK/VERSION；IPAM 委托；veth、/32/网关/邻居配置；事务与回滚；host 路由初始化函数 | CNI/netlink/netns 库、host-local、Linux 路由 | 同节点 Linux 路由通信；并发分配、重复 DEL、失败与 SIGKILL 后清理正确 |
| M2 同节点 eBPF 转发 | 6–9 日 | parser、Map ABI、本地端点表、CT guard、FIB 结果验证、TTL/校验和/MAC、redirect、目标检查与原因计数 | BPF helpers/kfunc、Linux FIB/邻居 | 真正通过 BPF 投递 TCP/UDP；正反向均有效；TTL 与内核基线一致；回退无二次修改；最小 NAT 夹具不被绕过 |
| M3 Agent 与 Kubernetes 集成 | 6–9 日 | Agent 引导；Node/Pod informer；UDS prepare/commit；身份与快照编译发布；节点路由；就绪/重启恢复 | client-go、Kubernetes PodCIDR 分配、containerd、kube-proxy | 真实 Pod 创建；PodIP 未回写可启动；内核跨节点基线可用；local-fast 下 ClusterIP、DNS、hairpin 初步通过 |
| M4 跨节点 Native 快转与 Service 门禁 | 7–10 日 | underlay ingress 分类、本地投递、远端输出约束；各入口 CT guard；异常路径与 Service 回包诊断 | 节点 underlay、Linux CT/NAT、kube-proxy iptables 模式 | 独立内核双节点 TCP/UDP 双向快转；完整 SVC-01 至 SVC-07；固定范围 NodePort；产出 R1 与初始性能数据 |
| M5 辅助 ACL 与生命周期可靠性 | 4–6 日 | 有版本 JSON 的无状态 Pod 对 ACL、两方向编译；目标最终检查；资源漂移/重启/有界诊断 | 现有快照与内核网络 | 快慢路径都不能绕过 ACL；失效接口/IP 复用不误投；100 轮生命周期无资源泄漏 |
| M6 VXLAN 第二路径 | 5–8 日 | 设备/路由/邻居/FDB 计划与恢复；BPF 输出到隧道与解封装入口；MTU 回退 | Linux VXLAN 封装/解封装 | 先 Linux 隧道基线，再 BPF 隧道路径；抓包证明内外层；重复 Service 与 ACL 验收 |
| M7 性能、回归与展示 | 5–8 日 | 可复现脚本、统计与剖析、单项优化、技术说明和演示 | iperf3/netperf/perf/bpftool 等 | B0–B3 对照、fast/slow 比例、原始数据；正确性回归；形成 R2 |

测试编号的唯一完整定义见 [测试计划](testing.md) 和 [Service 测试](service-compatibility.md)。时间包括理解、实现、调试和记录，不要求一次生成整套代码。

## 2. 阶段开始的第一项小任务

- M0：两个 netns 分别用 /32 和宿主路由接入；确认普通内核路径，再用一个 TC 程序把包重定向到目标 veth，并用目标 egress 的丢弃实验确认 hook。
- M1：先实现配置解析和 VERSION，再做一个可回滚的 ADD；资源创建之前先定义事务记录。
- M2：先完成固定两个本地端点的单向转发，验证 TTL/MAC 与接收结果；随后接双向、CT guard、回退与错误向量。
- M3：先让真实 runtime 与 Agent 完成一个 Pod 的 prepare/commit，确认不等待 Pod.status.podIP；再接 watch 和快照更新。
- M4：先只开启一对节点的 Native 快路径；对比开/关时跨节点 Service 的回包地址与 conntrack 计数。
- M5：先实现一对实验 Pod 的双向 TCP 拒绝规则，验证快速转发和普通内核转发都被目标检查拒绝。
- M6：先手工验证三类 VXLAN 转发表，再将理解后的配置转换成 Go 控制代码，最后开启对应 BPF 路径。
- M7：先得到可重复的 Linux 与完整 BPF 路径数据，再选择有剖析证据的一项优化。

## 3. 知识点随实现讲解

| 阶段 | 必须能解释 |
|---|---|
| M0–M1 | netns、veth 两端、TC 方向、/32 接入、网关静态邻居、CNI 调用与回滚 |
| M2 | skb、字节序、IPv4 checksum、TTL、MAC 下一跳、FIB 返回码、redirect、快慢路径 |
| M3 | Node/Pod 到转发表、Informer/队列、UID/epoch、不可变快照、资源所有权 |
| M4 | underlay、双向路由、Service DNAT/反向 NAT、conntrack 查找与引用、hostNetwork 边界 |
| M5 | 逐包 ACL 与有状态策略差别、重定向是否绕过 hook、重启与地址复用 |
| M6 | VTEP、路由/邻居/FDB、内外层地址、封装 MTU与隧道设备 |
| M7 | 运行路径证据、剖析、GSO/GRO、吞吐/延迟分布、CPU 与回退比例 |

## 4. 分级成果与缩减范围

- R0（M2）：自研 CNI + 同节点三层 eBPF 快转原型；不宣称 Kubernetes 全部集成。
- R1（M4）：自研 CNI/Agent + Native 同/跨节点快转 + kube-proxy Service 共存，作为首个完整项目成果。
- R2（M7）：增加辅助 ACL、VXLAN、可靠性和性能实验，作为完整推荐交付。

完整 NetworkPolicy、选择器编译、有状态 ACL、自研 TCP 授权流表/BPF timer、Service LB/NAT、XDP、BGP、DPDK/RDMA、自动混合选路均后置。用户投递提前时交付已经验收的 R0/R1，不把未运行功能写进成果。

CT 查询门禁不通过时仍可开发和展示隔离 netns 内的转发，但不得宣称已完成与 kube-proxy 共存；不能通过删掉回包检查来赶进度。目标 egress 检查门禁不通过时，先修正 hook 设计再继续。

## 5. 开源对照与记录

按对应阶段阅读固定 commit 的 CNI、Linux FIB/redirect/conntrack kfunc、Calico 三层接入与路由、Cilium 转发/Service 分工、Flannel VXLAN 控制。记录自己实现与复用的边界，不以“读过仓库”代替机制说明。

每阶段遵循主方案八条协作规则；每次只处理一个可验证的小任务。开源 issue/PR 必须来自真实问题，对外发布按用户授权执行，不作为本项目结束的外部依赖。
