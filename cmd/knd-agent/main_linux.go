//go:build linux

package main

import (
	"context"
	"flag"
	"kubenetdoctor/internal/agent"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	var o agent.Options
	flag.StringVar(&o.NodeName, "node", os.Getenv("NODE_NAME"), "Kubernetes Node 名称")
	flag.StringVar(&o.Underlay, "underlay", "underlay0", "节点间接口")
	flag.StringVar(&o.ServiceCIDR, "service-cidr", "10.96.0.0/12", "固定 IPv4 ServiceCIDR")
	flag.StringVar(&o.Mode, "mode", "kernel", "kernel、local-fast、native-fast、vxlan-kernel 或 vxlan-fast")
	flag.StringVar(&o.Kubeconfig, "kubeconfig", "", "直接 API Server 的 kubeconfig")
	flag.StringVar(&o.Socket, "socket", "/run/knd/agent.sock", "本地协议 socket")
	flag.StringVar(&o.StateDir, "state-dir", "/var/lib/knd/agent", "Agent 状态")
	flag.StringVar(&o.CNIStateDir, "cni-state-dir", "/var/lib/knd/cni", "CNI 事务状态")
	flag.StringVar(&o.CNIConfigDir, "cni-config-dir", "/etc/cni/net.d", "CNI 配置目录")
	flag.StringVar(&o.IPAMDir, "ipam-dir", "/var/lib/cni/networks", "host-local 租约目录")
	flag.StringVar(&o.PinDir, "pins", "/sys/fs/bpf/knd/v2", "专用 bpffs 目录")
	flag.StringVar(&o.Object, "object", "/opt/knd/datapath.o", "正式 BPF ELF")
	flag.IntVar(&o.MTU, "mtu", 1450, "Pod MTU")
	flag.Parse()
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := agent.Run(ctx, o); err != nil {
		log.Fatal(err)
	}
}
