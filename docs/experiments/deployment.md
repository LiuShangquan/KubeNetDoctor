# 最终部署与恢复实验

2026-11-17，两个独立内核 VM；最终模式 vxlan-fast，Agent 镜像 `knd-agent:v0.3`。镜像 OCI index：`sha256:a57414652a0f7b1e35eb39a09ac65b706b987955b82a9d7283f0eed5f558a1c9`；运行 manifest：`sha256:59baa73b4a762d0fb21cfa49bc7dad4135dfd7435bc00381ffbf162a6daf06f0`。不包含 B3 实验 ELF。

## DaemonSet

由 `scripts/deploy-lab-daemonset.py` 导入本地镜像、停用专用 VM 的 systemd Agent、应用 RBAC/DaemonSet。使用 hostNetwork，直接连接 192.168.77.11:6443，以 projected ServiceAccount token 认证，避免依赖尚未就绪的 Pod 网络/Service VIP。RBAC 仅 get/list/watch Node/Pod；网络管理容器需要本实验节点上的 privileged 权限。

initContainer 安装 CNI 二进制；宿主 CNI 目录 `/opt/cni/bin`、`/etc/cni/net.d`。持久状态和 bpffs 与宿主共享。netns 的 HostToContainer 挂载传播让 Agent 看见容器启动后 runtime 新建的 netns；`/run/knd` 和 netns 路径使用 DirectoryOrCreate。

`tests/kubernetes/daemonset.py` 实际通过：

- 两节点 Agent Ready；systemd knd-agent inactive/disabled。
- 在 Agent 容器启动后创建新的普通 Pod，runtime UID 和 Ready binding 对应，另一节点 Pod HTTP 可达；随后无附件残留。
- 删除并替换 node1 Agent Pod，已有端点 ID/epoch 保持，PodIP 和 VIP 继续可用。
- 双向跨节点包 TTL=62，源/目标 IP、MAC、checksum 与两节点 redirect 计数对应。
- 本地/远端/hairpin ClusterIP TCP/UDP、DNS TCP/UDP、已有 Service TCP 连接与新后端选择、限定外部 NodePort TCP/UDP 通过。

完整结果：[DaemonSet-results.json](DaemonSet-results.json)。测试名称中的 `native_two_hop_fast_path` 是复用函数名，最终本次运行 mode 为 vxlan-fast，不能据此误读为 Native 测量。

## API 短时不可达

`tests/kubernetes/api_outage.py` 只在专用 node2 添加带项目注释、精确目的地址/端口的临时 OUTPUT REJECT，阻断到 API 的 TCP。实际观察 apiReachable=false 和错误文本；已有 a→c PodIP HTTP 继续成功。对已有附件重新发起需要 fresh API 核验的 prepare 得到 HTTP 409，不改变附件；恢复规则后健康探测恢复。

脚本 finally 删除自己的精确规则；没有删除其他规则或清空 conntrack。结果：[API-outage-results.json](API-outage-results.json)。此实验是短时失联，不覆盖长时 watch 重连、对象全量重建或集群控制面 HA。

## 最终回归与状态

最终源码通过 `make build bpf test vet`，包括 Go race 测试、BPF 编译和 go vet；正式默认 ELF 重新通过全部 M2 用例。记录：[Final-regression-results.json](Final-regression-results.json)。当前状态采样由只读 `scripts/capture-lab-status.py` 生成：[Final-lab-status.json](Final-lab-status.json)。RSS 为时点值，不是压力峰值。

重启中遇到的目录问题已修复，一次 guest kernel panic 的根因仍未知，见 [环境故障记录](environment-incidents.md)。Agent Pod 替换、guest 重启和 BPF 程序升级是三个不同问题：同 ABI 重启复用 pin，镜像变化不会自动热更新已挂接程序。

## 复现

```bash
# 在 WSL，当前模式已准备、对应镜像 tar 已构建时：
python3 scripts/deploy-lab-daemonset.py --mode vxlan-fast --tag v0.3
python3 tests/kubernetes/daemonset.py
python3 tests/kubernetes/api_outage.py
python3 scripts/capture-lab-status.py
```

镜像在 Windows Docker Desktop 构建：`docker build -f deploy/agent.Dockerfile -t knd-agent:v0.3 .`，再 `docker save -o build/knd-agent-v0.3.tar knd-agent:v0.3`。这些是专用实验环境脚本，不是可无条件应用于其他集群的通用安装器。
