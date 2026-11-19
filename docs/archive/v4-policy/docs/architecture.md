# 组件、接口与生命周期设计

状态：设计合同，所有接口均为本项目拟定义接口，不代表已有库 API 或已实现代码。总览见 [../设计方案.md](../设计方案.md)。

## 1. 组件与所有权

| 组件 | 输入 | 输出 | 独占管理的对象 |
|---|---|---|---|
| knd-cni | CNI 配置、环境参数、netns | CNI Result/标准错误 | 本次附件的 veth、地址、Pod 内路由、IPAM 租约与事务记录 |
| knd-agent | Kubernetes 对象、本机附件登记 | 端点配置、路由、策略快照 | 本地 bridge/VXLAN、项目路由/邻居/FDB、BPF program/map、端点状态 |
| TC 程序 | host-side veth 上的 skb、Map | OK/SHOT、流状态、计数 | 数据面流状态与计数；不写用户态策略快照 |
| kndctl | 管理员查询 | 版本、端点、Map 摘要、计数 | 不拥有网络配置 |

主进程只有节点 Agent；没有诊断 Manager、中央报文服务、数据库或消息队列。Agent 停止后现有内核转发与已挂接程序继续工作；控制面停止更新，新受管附件不能接入。重启/资源压力下的具体限制见 [known-limitations.md](known-limitations.md)。

## 2. 引导顺序

1. 安装器在隔离集群部署 hostNetwork Agent，配置真实可达的 API Server 地址与只读资源权限，避免依赖尚未建立的 Pod 网络/ClusterIP。
2. Agent 检查 Node UID/PodCIDR、underlay 接口与地址冲突；固定单网卡/单 IPv4 PodCIDR。
3. 打开本地状态目录与 bpffs，对已有资源核对所有权、ABI 和 Node/boot 标识。
4. 建立 bridge/网关，加载 TC 程序模板与 Map；等待所需 informer 同步，形成初始策略快照。
5. 原子发布 CNI 配置并开放本地端点注册；路由收敛、策略缓存同步、本地 CNI 就绪分别报告，不合并成模糊的 Ready。
6. runtime 调用 CNI；CNI 与 Agent 协作登记和挂接端点。

M1 的无策略 CNI 验证使用明确的 `connectivity-only` 实验配置；M5 的 `policy-enforced` 配置要求策略挂接完成才返回 ADD 成功。两个模式不能在日志中混淆，也不在已运行受管 Pod 上悄悄切换。

## 3. 身份与持久状态

| 身份 | 用途 | 不能替代什么 |
|---|---|---|
| networkName + sandboxID + ifName | CNI 附件事务键 | 不能只用 Pod 名称 |
| Pod UID | Kubernetes 对象生命周期 | 不等于 IP 或 Cilium security identity |
| local endpointID + attachEpoch | 本节点 BPF/流状态隔离 | 不能跨节点比较 endpointID |
| Node UID + bootID | 资源与内核实例范围 | 节点重建不能沿用旧 ifindex |
| PeerToken | 根据已确认 Pod UID 编码的固定长度身份 | 不证明数据包携带了该身份 |
| ifindex / netns inode | 识别当前内核资源 | 均可能复用，不能作为永久身份 |

endpointID 与 attachEpoch 来自持久递增分配，首次安装/状态不兼容时引入新的 agent instance 标识并清空不可信旧状态。PeerToken 使用 Pod UID 的规范化编码；若需要摘要，必须检查候选集合的碰撞并拒绝冲突，不能静默合并。

本地状态目录候选 `/var/lib/knd/`；权限仅 root，按附件保存原子替换的 JSON 事务记录及节点状态。记录 schemaVersion、完整身份、配置摘要、分配结果、资源别名、阶段与最后错误。bpffs 候选 `/sys/fs/bpf/knd/`，使用 ABI 版本子目录，不把任意同名 Map 当成本项目资源。

本项目首次支持的 containerd 组合必须在 M0 验证：CNI_CONTAINERID 与 sandbox 的关系，以及 CNI_ARGS 中 Pod UID/name/namespace 的实际传入。设计优先要求可信 runtime 传入 Pod UID，再与 API 中同 UID、同节点对象核对。若该组合没有 UID，不通过 Pod 名称猜测，先记录不支持并另立 runtime 适配决策；首版不为了查身份挂载具有管理权限的完整 CRI socket。

**ADD 期间不能等待 Pod.status.podIP 出现。** IP 是本次 IPAM 刚分配的，API 状态可能在 ADD 完成之后才更新。本地 IP 映射以已核验 UID 的附件记录先行建立，随后与 API 状态核对；发现冲突则隔离端点并报错。

远端 Pod 身份主要来自 API IP→UID 映射，天然有传播延迟。其限制、旧流失效与同 UID sandbox 重建的边界见数据面文档，不承诺从 IP 单独恢复可靠的历史身份。

## 4. CNI↔Agent 本地协议

选择 **HTTP/JSON over Unix domain socket**，使用 Go 标准库，暂不引入 gRPC/Protobuf。候选 socket `/run/knd/agent.sock`，仅 root 可访问；Agent 核验对端进程身份及网络资源，不信任任意请求提供的路径或 ifindex。

共同请求字段：`apiVersion`、`requestID`、附件事务键、Pod UID、Node UID、configDigest。大小上限候选 64 KiB、请求超时 10 秒；具体 runtime 超时必须在 M0 验证。幂等的是协议重试，不擅自改变 CNI 规范对重复 ADD/已有接口的要求。

| 接口 | 关键输入 | 返回 | 语义 |
|---|---|---|---|
| POST /v1/endpoints/prepare | 附件身份、分配 IP、hostIfIndex、接口 alias、netns 的已验证身份 | endpointID、attachEpoch、transactionID、状态 | 验证资源并登记 Pending，默认隔离，挂接项目 TC 程序 |
| POST /v1/endpoints/{id}/commit | transactionID、configDigest | Ready、appliedGeneration | 在核验的快照中激活端点；同事务重复请求返回相同结果 |
| GET /v1/endpoints/by-attachment | 附件事务键 | 当前事务/端点状态 | 请求超时后确定实际状态，不能按超时推断未执行 |
| DELETE /v1/endpoints/{id} | transactionID 或匹配的附件身份 | 删除/已不存在 | 先阻断端点再失效关联状态，幂等 |
| GET /v1/status | 无 | informer、网络、策略、ABI、desired/applied | 分项报告，可用于调试工具 |

错误类别：InvalidConfig、Unsupported、IdentityConflict、NotReady、ResourceConflict、CapacityExceeded、RetryableIO、Internal。返回稳定 code 与可读信息，CNI 再映射为标准错误结构；错误信息不得把内部任意路径操作暴露为接口能力。

## 5. ADD / DEL 状态机

```text
ADD: Validated → IPAllocated → LinksPrepared → AgentPrepared
     → PolicyCommitted → LinksEnabled → ResultWritten
失败: 根据事务记录反向清理本次创建的资源，保留需要重试的错误

Agent: Pending → AttachedBlocked → Ready → Quarantined → Deleting → Deleted
```

ADD 过程中先准备未向业务开放的接口，挂接与默认隔离生效后才允许启用链路；Agent commit 的 Ready 表示策略已发布，CNI Result 仍需等接口、地址、路由读回成功。对接口 UP/策略挂接之间的竞态必须做持续发包测试。

Agent prepare/commit 超时后，CNI 查询事务状态。无法确定时按失败处理并清理自己的链路；Agent 后续发现资源不存在要撤销登记。不能因重试多分配一个地址或创建第二套端点。

DEL 顺序：要求 Agent 阻断并失效授权 → 删除确认属于该附件的 veth → 释放 IPAM → 清理事务记录。Agent 不可达时，CNI 仍可删除经过所有权核验的链路来切断旧附件，并记录 tombstone 供恢复；不能在旧附件是否已断网不明时先把 IP 重新分配给别人。

netns 已消失时，仍通过 host veth alias/事务记录核验清理并释放 IPAM。资源身份冲突时不盲删，返回错误并保留证据。共享 bridge、VXLAN 和其他端点不由单次 DEL 删除。

进程 SIGKILL 无法依靠 defer 回滚。事务记录和后续 DEL/恢复扫描负责识别残留；没有可靠存活证据时不按“超时”自动回收活跃地址。此恢复逻辑不等于 CNI 1.1 GC。

## 6. 控制面输入与协调

观察 Node、Pod、Namespace、NetworkPolicy，按需要建立节点、UID、IP 和 Namespace 索引。Informer 回调只入队，编译读取最新缓存；更新风暴使用候选 50ms 合并窗口、最长 200ms 等待。

本地编译输入是复制出的不可变对象集合，记录各对象 UID/resourceVersion；不同资源的 resourceVersion 不做数值大小比较。复制期间收到相关变化则排队再编译，不声称获得 API 全局事务快照。

Agent 单一发布者串行化路由计划和 BPF 发布各自的写操作。纯编译计算可取消旧任务，但新版本应用必须受序号约束，避免慢的旧编译覆盖新配置。

正常过程：

```text
Cache synced → Validate → Compile → Capacity check → Stage new Map
 → Verify staged entries → Publish reference → Read back generation → Report applied
```

API 暂不可用时保留已提交规则与路由，报告 stale；新端点无法核验则拒绝接入。已有连接与新连接按最后已提交策略运行，不能称为持续执行了 API 中最新策略。

## 7. 策略发布与隔离

完整规则与必要身份条目放入同一 immutable inner snapshot；outer map 的单项切换只提供节点内快照引用替换。一个 TC invocation 获取一次引用，后续查表保持同一快照。

端点 binding/紧急隔离标志与流状态是独立 Map。它们不是与策略快照一起原子提交：端点新增/删除采用“先阻断、后发布、再开放”的顺序；规则升级只改变快照。已经进入旧程序执行的一小批报文可以按旧版本结束，不能承诺切换瞬间所有 CPU 同步。

编译失败、非法字段、容量不足时不发布半成品；按已定义范围隔离受影响端点，再报告失败。隔离写入本身也需要检查返回值与读回，失败时必须显示“限制未确认生效”，不能靠报错日志宣称 fail-closed 已完成。过载期间禁止新附件接入。

正常标签/允许规则更新不强制中断已授权连接；端点删除、身份冲突或显式异常隔离应优先于旧流授权。具体顺序见 [datapath-and-policy.md](datapath-and-policy.md)。

## 8. 重启、权限与资源

- 保存 schema/ABI、Node UID、bootID、程序标识和 Map 元数据。相同内核实例重启可核验后恢复 pinned 对象；ABI 不兼容时需维护窗口与明确迁移，不假装无损升级。
- 无法确认旧 endpointID/epoch 的进程恢复先保持阻断，重建附件映射；不能复用新的 ID 解释旧流。
- 节点重启后内核对象按新实例重建，不恢复旧连接授权；业务重新建立连接。
- Agent 只读 Kubernetes 所需资源，不默认授予 cluster-admin；策略状态先通过本机接口报告，不虚构 NetworkPolicy.status 字段。
- 实验初期 privileged 若确实需要则记录，后续验证 net_admin/BPF/宿主挂载等最小集合；权限收敛是验收项，未实测不宣称最小权限可用。
- Map 容量、单次 API 大小、编译队列和工作线程均有上限；调试工具不提供任意 Shell/网络配置执行入口。

所有接口与目录是设计约定；开始实现时将契约测试与失败路径一起落实，不能只实现成功路径。
