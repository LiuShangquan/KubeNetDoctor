# 环境故障与恢复记录

## Guest kernel panic：根因未确定

2026-11-17（北京时间）实验期间，node1 Linux 7.0.0-43-generic 在 PID 1 systemd 的 openat 路径中崩溃。原始控制台日志位于本机 `build/kernel-crash/node1-panic.log`，关键堆栈见 [保留摘录](kernel-panic-excerpt.log)。包含 `general protection fault`、`path_is_under`、`complete_walk`、`do_open` 和 `Kernel panic - not syncing: Fatal exception`。

发生时正在检查/添加 bpffs 启动配置；这只是时间关联，没有最小复现或内核诊断证明因果。不能据此归因于 eBPF、systemd、QEMU 或某条命令。QEMU 配置 no-reboot，guest 退出后 VM 管理器也停止另一节点；随后使用原磁盘重新启动。

Ubuntu 已自动挂载 bpffs，因此撤掉了多余的启动配置方案。恢复后确认 `/sys/fs/bpf` 文件系统为 bpf_fs，Agent/Pod 网络恢复、Native/VXLAN 测量和最终功能回归继续执行。**没有证明内核问题已修复，尚未完成长期 soak。**

## 重启后 hostPath 不存在：已修复项目部署

重启清空 `/run`，原 DaemonSet 的 `/run/knd` hostPath 使用 `Directory`，导致容器无法启动，CNI 无法连接 Agent。修改 `scripts/deploy-lab-daemonset.py` 及生成清单：socket 和 netns 目录使用 `DirectoryOrCreate`，netns 保留 `HostToContainer` mount propagation。

应用后两个 Agent 恢复 Ready，kubelet 重建演示 Pod sandbox 并重新调用 CNI，重新建立 veth/IPAM/绑定。之后还通过新 sandbox 创建和 Agent Pod 替换实验。未据此承诺任意重启/断电情况下无损。

## iperf3 pthread_cancel：AppArmor 测试例外

跨节点 iperf3 3.19.1 在数据传输后退出失败：`sender cancel in pthread_cancel - unable to cancel thread: Permission denied`。TCP 已传数据但进程不结束，复用 server 又污染后续 UDP。

Guest 审计明确出现 AppArmor signal deny：profile `cri-containerd.apparmor.d`，comm `iperf3`，signal `rtmin+1`，peer 为堆叠 profile `cri-containerd.apparmor.d//&unconfined`。当时容器 Seccomp 字段为 0，不能将此错误归因于 seccomp 或 BPF 转发。

处理仅限 `tests/benchmark/cross_node.py` 创建的 bench-a/bench-c：配置 `appArmorProfile.type: Unconfined`，每轮启动独立 one-shot server，设置超时并核验 PID cmdline 后清理。测试结束删除两个 Pod；普通 a/b/c 和全局 AppArmor 设置不变。之后两种模式各六次传输均正常结束。保留 [审计摘录](apparmor-excerpt.log)。
