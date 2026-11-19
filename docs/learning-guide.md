# 从一个 Pod 到一次转发

目标是能自己解释和修改网络路径。先阅读代码，再在保留的专用集群观察对应对象；不需要另外写 TCP 收发练习。每节的“检查理解”可作为后续一起学习的小任务。

## 1. 先划清三个组件的职责

**CNI** 是 runtime 调用的一次性程序。containerd 已创建 sandbox network namespace，把容器 ID、netns 路径、接口名和 Kubernetes 元数据交给 `knd-cni`。我们的插件委托 host-local 取得 IP，创建 veth，把一端放入 netns，配置 /32、网关路由和静态邻居，并通过本地 Unix socket 向 Agent 登记。它最终向 stdout 输出 CNI JSON 结果，然后退出。

**Agent** 是节点上持续运行的控制程序。它从 Kubernetes 得到 Node/Pod，从 CNI 得到本地附件；两者通过 UID、节点、接口和持久事务关联。它把这些信息转换为 Linux 路由/邻居/隧道和 BPF Map，决定哪些附件已经具备收发资格。它不逐包转发，停止 Agent 后已经挂接的 BPF 与路由仍在内核。

**eBPF** 是内核在 TC hook 调用的 C 程序。它读取 Agent 发布的状态，对当前 skb 作出 FAST、SLOW 或 DROP 决定。FAST 会查询 FIB、核验输出、减 TTL、修正 IPv4 checksum、改写以太网 MAC，最后 redirect；SLOW 保持原包交给 Linux；DROP 是拒绝。它不 watch Kubernetes、不分配 IP、不创建 netns。

检查理解：为什么 CNI 进程退出后 Pod 还能通信？为什么只写 eBPF 程序还不能完成 CNI ADD/DEL？

## 2. 接入拓扑：为什么是 /32

每个 Pod 的 eth0 只配置自己的 /32，默认路由经 `169.254.1.1`。该网关永久邻居指向它自己的宿主 veth MAC。宿主维护 `PodIP/32 dev kn...` 路由和目标 Pod MAC。没有 Pod bridge，同节点 Pod 之间也走三层转发。

阅读 `internal/cni/commands_linux.go`、`internal/network/network_linux.go`。在节点上看 `ip -d link`、`ip route show proto 190`、`ip neigh`；在 Pod 中看 `ip addr`、`ip route`、`ip neigh`。节点 PodCIDR 是 IP 分配池，Pod /32 是接口接入方式，二者不矛盾。

检查理解：同节点目的 IP 为什么也先发给网关 MAC？IP 地址与下一跳 MAC 分别决定什么？为什么需要本地 PodCIDR blackhole 路由？

## 3. ADD 不是一串无条件命令

阅读 `internal/cni/state_linux.go` 和 `commands_linux.go`。创建资源之前写持久 journal，记录附件 key、IP、接口名、所有权和阶段。锁控制本机并发，fsync/rename 保证记录落盘。失败后按记录和实际内核对象核验，不能看到相同接口名就删除。

ADD 的重要顺序是：取得 IP → 创建 host 端保持 DOWN 的 veth → Agent prepare 先发布不可用 binding → 启用链路 → commit 核验并安装路由、发布 Ready → 返回结果。prepare 阶段必须从 API 新鲜读取 Pod UID，不能等 `Pod.status.podIP`；runtime 正在等待 CNI 完成才会回写 Pod IP。

DEL 支持重复调用和 netns 已消失。Agent 不可达时先核验并删除自己拥有的 veth，随后释放租约，保留 detached journal 供 Agent 回收；不能提前释放仍然活跃接口占用的 IP。看 `tests/cni/run.py` 的失败注入，理解测试为什么要在分配、建链、prepare、Ready 等阶段中止。

检查理解：为什么 IP 释放应晚于接口清理？commit 回复丢失后如何避免生成第二个附件？为什么接口名和 ifindex 都不足以证明所有权？

## 4. Agent 把对象翻译成转发表

阅读 `internal/agent/agent_linux.go`、`store.go`、`internal/datapath/abi.go`。Informer 触发有界队列，串行控制循环构建当前期望状态；定期重查处理遗漏和漂移。fresh API Get 用于新增附件身份验证，已有转发可在 API 短暂失联时保留。

一个 Pod 有多个不同的身份：Kubernetes UID 表示对象；附件 ID/epoch 表示该节点一次接入；IP 用于包头查找；ifindex 用于操作设备。重建后 IP/ifindex 可能复用，UID/epoch 防止旧本地状态被当成新附件。网络包本身不携带 UID，所以项目不宣称解决所有在途旧报文与 IP 复用歧义。

快照使用新建、填充、freeze 的 HASH，再切换 `ARRAY_OF_MAPS` 的引用。单次 BPF 调用拿到完整一代；路由、binding 和不同节点之间仍不是全局事务。因此发布时保守关闭加速、目标重复核验当前 binding，故障时读回阻断状态。

检查理解：为什么不能一条条原地覆盖正在使用的完整快照？为什么 API 失联时保留旧路径却拒绝新 prepare？

## 5. 同节点快路径：逐行对应包头变化

从 `bpf/datapath.c` 的 `knd_from_pod` 开始，沿解析、资格判断、CT 查询、FIB 转发函数读到 `knd_to_pod`。假设 A→B，初始 TTL=64：

1. A 发出 IP A→B，以太网 A-MAC→host-veth-A-MAC。
2. host-veth-A ingress 核验当前 binding、源 IP/MAC、头部边界和校验和。Service、未知目的、合法但不支持的特殊包走 SLOW。
3. 当前 tuple 的 conntrack 结果必须是明确 miss；hit/error 都 SLOW。
4. FIB 返回 B 的宿主 veth 和邻居 MAC。程序再次对比快照、接口、ID/epoch，防止把不一致路由当成成功。
5. 完成所有可能回退的检查后，TTL 64→63，IPv4 checksum 相应更新，以太网变为 host-veth-B-MAC→B-MAC；IP/端口不变。
6. `bpf_redirect(ifindex, 0)` 请求目标设备 egress。`knd_to_pod` 再检查当前附件、目标地址/身份与 ACL，然后 Pod B 收到。

改包后的失败必须 DROP，不能把已经减过 TTL 的包再交内核继续路由。M0 的目标 hook 注入 DROP 实验用于证明 redirect 确实经过此门禁；只看到 redirect 计数增加不能证明收包成功。

检查理解：为什么既检查源入口又检查最终目标？MAC 改写和 IP NAT 有什么区别？为什么 SLOW 必须发生在改包之前？

## 6. 跨节点的 Native 与 VXLAN

Native 的远端 PodCIDR 路由指向目标节点 underlay IP。源节点 BPF 改写为节点间 MAC 并输出，目标节点 `knd_from_network` 只接收可信拓扑中的远端 Pod→本地 Pod，再投递目标 veth。两次三层转发使测试初始 TTL 64→62。

VXLAN 中远端 PodCIDR 指向 `kndvx0`，VTEP 是每节点 PodCIDR 基址 /32。Agent 配置 VNI 42、UDP 4789、静态邻居与 FDB；BPF 把内层包 redirect 给隧道设备，Linux 负责外层封装。远端 Linux 解封装后，`kndvx0` ingress 的 BPF 执行本地转发。underlay MTU 1500，Pod/隧道 MTU 1450。

阅读 `internal/network/vxlan_linux.go` 与 `tests/kubernetes/vxlan.py`，同时看内层 PodIP 和外层 NodeIP。VXLAN 是传输封装，eBPF 是执行转发逻辑的机制；可以同时使用。Calico/Cilium 是完整网络系统，不能把两者分别简化成 VXLAN/eBPF。本项目没有实现 VXLAN 协议封解装本身。

检查理解：为什么有 route 仍可能因缺 neighbor/FDB 而不可达？为什么 VXLAN 内层 MTU 更小？

## 7. Service 为什么必须检查回包

请求 A→VIP 经 kube-proxy DNAT 为 A→后端 B。B 的回包是 B→A，此时目的已经不是 VIP。如果只排除 VIP，BPF 可能跳过需要反向 NAT 的节点，客户端看到 B 而不是 VIP。

因此每个拟快转入口都查询本机 conntrack，命中或查询错误交给 Linux；只允许明确 miss 的普通 Pod 流进入 FAST。还保留 NodePort、hairpin SNAT、DNS TCP/UDP 与后端变更实验。某直连流若曾经走内核建立 CT，后续可能一直 SLOW，这会影响加速比例。

本实验 kube-proxy 运行 iptables 模式，iptables 工具使用 nf_tables 后端；这不等于 kube-proxy 原生 nftables 模式。IPVS 也没有被用作本项目的数据面。详见 [Service 合同](service-compatibility.md)。

检查理解：VIP 排除与 CT guard 各覆盖什么？为什么不能把 CT error 当成 miss？

## 8. ACL、失败和测量

`internal/agent/policy.go` 的规则按明确源/目标 UID 和 TCP(6)/UDP(17) 定义有向拒绝，Agent 解析成当前地址对，目标 veth 对快慢路径执行相同规则。双向隔离需两条规则；接口没有自动 namespace 白名单，实验只对 `knd-test` 使用，root 调用者必须选对 UID。SNAT 后可能失去原始身份，所以这不是完整 NetworkPolicy。

先读 [M5](experiments/M5.md) 中 IP 复用、所有权漂移和 100 次 Pod 创建删除，再读 [环境故障记录](experiments/environment-incidents.md)。已验证功能不等于所有环境稳定：本次出现过一次原因未明的 guest kernel panic，重启还暴露了 `/run` 目录恢复缺陷，后者已修复。

最后读 [M7](experiments/M7.md)。B0 是 Linux 基线，B1 保留检查但强制慢路，B2 是完整交付路径，B3 是只用于实验的优化候选。数据未证明 B3 稳定收益，最终没有采用。解释没有收益的原因，比把任何 eBPF 项目都称作“高性能”更有工程价值。

检查理解：100 次生命周期证明了哪些资源回收行为，不能证明什么？应用 RTT、BPF 纳秒耗时和吞吐是否可以相互替代？共享宿主 VM 的测试为何不能代表物理网络线速？
