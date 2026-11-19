# 组件、接口与生命周期（v5）

状态：2026-11-17 已实现的组件与接口。CNI、Agent、Native/VXLAN 数据面、ACL 与 DaemonSet 均有实测记录。实际边界以 [实施状态](implementation-status.md) 和阶段实验记录为准。总览见 [设计方案](../设计方案.md)。

## 1. 系统组成与所有权

| 组件 | 自己开发的职责 | 明确复用/不拥有的内容 |
|---|---|---|
| knd-cni，Go 按需进程 | CNI 参数/结果、IPAM 委托、veth、Pod /32 地址/路由/邻居、附件事务、ADD/DEL/CHECK | runtime 创建 sandbox/netns；host-local 分配地址 |
| knd-agent，每节点 Go 常驻进程 | Node/Pod 同步；本地端点登记；内核 /32 与节点路由；VXLAN；BPF 加载/挂接/快照与恢复 | 不执行逐包 Go 转发，不编译 Service 后端表 |
| TC BPF，C 内核程序 | 入口核验、快慢路径选择、FIB 查询、TTL/MAC/校验和处理、redirect、目标检查、辅助 ACL | FIB/邻居、网卡驱动、完整 TCP/IP 栈、Linux CT/NAT |
| kube-proxy | 项目复用组件，配置 Service 网络 | 其链、地址转换与后端选择不由 knd-agent 接管 |
| kndctl | 查看端点、路由计划、能力和原因计数；发布本节点 UID ACL | 不提供任意 Shell 或删除他人网络资源 |

项目数据面由 BPF 快路径和 Linux 慢路径组成。CNI/Agent 处理配置，报文不经过 Unix socket 或 Go 服务。

## 2. 控制面输入与输出

主输入是 Node 和 Pod；Namespace 名称来自 Pod 元数据，实验范围固定。初版不监听 NetworkPolicy；Service/EndpointSlice 由 kube-proxy 处理。ServiceCIDR 与运行模式作为经过检查的静态配置传入。

```text
Node/Pod informer + 本机附件登记
 → 复制不可变输入
 → 校验 UID、IP 唯一性、NodeCIDR、接口所有权
 → 生成 KernelRoutePlan 与 ForwardingSnapshot
 → 按资源依赖顺序提交内核配置
 → 构建新 inner map，校验后切换引用
 → 读回并报告 localReady / fastReady / appliedGeneration
```

转发表编译负责把 Kubernetes 信息变成有界网络状态，包含 IP→Pod 身份、Pod→节点、本地目标接口/epoch、远端下一跳和允许的输出设备。真正的最长前缀路由查找复用 Linux FIB，不再维护第二套自研 LPM 路由算法。

Informer 回调写入容量 1 的合并通知队列；同节点写入持 mutex 串行执行，另有 2 秒周期核对。未实现原候选的 50ms/200ms 防抖器。不同资源 resourceVersion 不做数值大小比较，也不宣称 API 全局事务。

## 3. 引导与就绪

1. 在专用集群部署 hostNetwork Agent，使用实际 API Server 地址与所需只读 RBAC，避免先依赖 ClusterIP。
2. 核查 Node UID、PodCIDR、NodeIP、单 underlay 网卡、ServiceCIDR 不冲突及全局模式。
3. 核验本地状态、bpffs ABI、Node/boot 标识；准备网关 dummy、内核路由及受限转发配置。
4. 首次缓存同步完成后加载入口/目标程序与 Maps。缺 CT kfunc 时加载无 CT 引用的 baseline ELF，保持端点检查；其他 verifier/权限错误直接报告，不一概掩盖。
5. 发布 CNI 配置并接受本地登记。localReady 表示附件与内核基线可配置；fastReady 由选定模式、内核能力与本次快照发布控制。Service 门禁是独立验收脚本，不是 Agent 启动时自动执行的完整测试；不能仅凭 Ready 就声称所有 Service 场景通过。

默认模式为 `kernel`；M2 先启用 `local-fast`，M4 才启用 `native-fast`，M6 增加 `vxlan-fast`。这些是受控实验配置；不承诺在线无损切换。紧急关闭加速保留目标检查，不能顺手卸载隔离规则。

## 4. 身份、接口与持久化

附件事务键是 `networkName + sandboxID + ifName`；身份包含 Pod UID、Node UID、local endpointID/attachEpoch、bootID、接口 alias、IP/MAC 与配置摘要。ifindex、IP、Pod 名称不能单独充当长期身份。

M0 核查实际 containerd 传入的 Pod UID。优先使用可信 runtime 参数并与 API 同 UID、同节点 Pod 核对；若缺少 UID，先另立适配决策，不凭名称猜测，也不默认挂载完整 CRI 管理 socket。

ADD 期间使用本次 IPAM 地址登记本地端点，不能等待 Pod.status.podIP；后续 API 回写与其核对。远端端点来自 API 的 IP→UID→Node 映射，存在传播延迟。

状态目录为 `/var/lib/knd/agent` 和 `/var/lib/knd/cni`，root-only 原子替换 JSON；记录 schema、附件阶段、资源所有权与身份，当前错误由 status 显示。bpffs `/sys/fs/bpf/knd/v2`；持久分配 endpointID/epoch。boot 变化会清理持久附件/路由记录，Node UID 变化拒绝启动，避免把旧 ifindex 当作可信新状态。

## 5. CNI↔Agent 本地协议

HTTP/JSON over root-only Unix socket `/run/knd/agent.sock`，复用 Go 标准库。CNI 请求含 apiVersion=v1、key、containerID、Pod namespace/name/UID、endpoint、configDigest 与提交时的 transactionID；Node UID 由 Agent 本节点状态核验。上限 64 KiB、客户端超时 10 秒，SO_PEERCRED 要求 UID 0。未知字段、尾随第二份 JSON 被拒绝。

| 接口 | 输入与结果 | 保证 |
|---|---|---|
| POST /v1/prepare | DOWN 状态 host veth 与附件身份 → ID/epoch/transactionID | fresh API GET 核验 Pod UID，挂 TC、发布 Blocked binding |
| POST /v1/commit | 同一附件与 transactionID → ready、generation | fresh GET，配置宿主 /32 路由与邻居，检查内核配置后发布 Ready |
| POST /v1/endpoint | 完整附件身份 → ready、ID/epoch/generation | CHECK 与重试核验，不凭名称命中其他附件 |
| POST /v1/delete | 同一附件身份 → 已阻断状态 | 标记 Deleting；链路由 CNI 删除，后续 reconcile 回收记录 |
| GET /v1/status | 能力、缓存、路由、ABI、fastMode、desired/applied | 区分基线可用与快路径已验证 |
| POST /v1/policy | apiVersion=v1、rules、blockedUIDs | 校验 UID 方向性 TCP/UDP 规则，发布后返回 generation；本地阻断回读 binding |

实际错误使用 HTTP 400（解析/策略输入）、409（附件操作冲突/身份核验失败）、500（发布/读取失败）和 JSON error 文本；尚未增加设计中的细分枚举。策略输入错误保留旧规则；若提交阶段失败，desired/applied 与 error 显示未收敛，不能把新策略视为已生效。

## 6. ADD / DEL

```text
ADD：Validated → IPAllocated → LinksPreparedDown → AgentPreparedBlocked
 → LinksEnabledWithBlockedBinding → KernelAndIdentityCommitted → ResultWritten

端点：Pending → AttachedBlocked → Ready → Blocked/Deleting → Deleted
```

CNI 负责 Pod 内 /32、网关主机路由、默认路由与静态邻居；Agent 负责宿主 /32 路由、宿主邻居和 BPF 状态。M1 可使用测试夹具调用同一初始化/路由函数，M3 合入常驻 Agent，不维护另一个永久引导服务。

先挂接阻断检查再启用链路，读回配置后返回结果；不能让尚未登记的 veth 短暂开放。CNI Result 返回最终 /32/路由，不直接输出未规范化的 IPAM 结果。

失败时按事务记录回滚本次资源；prepare/commit 超时先查实际状态，仍不确定则关闭并删除自己拥有的链路，再回收地址。SIGKILL 留下的事务由后续 DEL/恢复扫描处理，不能依赖 defer。

DEL：Agent 阻断端点并撤快照资格 → 撤 host 路由/邻居 → CNI 删除匹配身份的 veth → 释放 IPAM → 清理事务。Agent 不可达时仍可删除已核验的自有链路并留 tombstone；接口删除后其关联内核对象应读回确认，不能在旧附件是否断网不明时先复用 IP。

## 7. 发布顺序与故障行为

- 新增：先有慢路径和目标检查，再发布快路径资格；移除：先关闭资格/阻断目标，再删路由和接口。
- Map 与路由不是联合事务。BPF 用 FIB 结果与 snapshot 交叉核对，失配走慢路径；目标 egress 再检查当前附件。
- 快照编译/容量等预校验失败不触碰 live maps，保留最后完整配置并报错；不能声称已关闭原有快转。进入发布写入后先关闭 fastReady，再有序更新。无合法 baseline/binding 的新附件拒绝接入。
- ACL 更新失败保留旧完整规则并显示 rejected；不能声称新拒绝规则已生效。附件紧急阻断单独执行并核验。
- API 每 5 秒进行一次独立、2 秒超时的 Node GET 探测，status 报告 apiReachable/lastAPIProbe/apiError。断连保留已提交内核状态，新 prepare/commit 的 fresh GET 失败，不把 Node NotReady 当作删除。
- Agent 进程退出时，已固定的内核对象可继续工作；重启先核验身份和 ABI。内核重启或不兼容升级不保留旧内核状态，不承诺连接无损。

## 8. 最小工程组织

```text
cmd/knd-cni/          CNI 入口
cmd/knd-agent/        节点主进程
cmd/kndctl/           诊断与本节点策略发布
internal/cni/        附件、IPAM、事务
internal/network/    三层接入、路由、邻居、VXLAN
internal/agent/      informer、身份、事务协议、状态持久化、UID ACL
internal/datapath/   加载、挂接、ABI、能力探测、Map
bpf/                 datapath.c 与 ABI 头文件
tests/               包向量、Linux、Kubernetes、性能
```

权限、资源预算和所有权均需实测；不预设 cluster-admin，不管理 kube-proxy 链，不建独立 Manager、数据库或 Web 平台。

实现集中在少数可追踪模块，未为原计划中的每个概念单独建包。DaemonSet 使用 hostNetwork/hostPID、受限只读 Node/Pod RBAC、projected SA token、固定实际 API 地址；privileged 仅用于本实验节点的网络管理。`/run/knd` 和 `/var/run/netns` 用 DirectoryOrCreate，netns 挂载 HostToContainer，已实测新 sandbox 可见及重启恢复。

ABI 2：key 16、value 64、config 32 字节。同 ABI 的 pinned 程序在进程重启时复用，镜像更新不会自动热替换内核程序；程序/ABI 升级须受控回收附件、移除所属 hook/pin 后加载。未实现在线程序迁移。
