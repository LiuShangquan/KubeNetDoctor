//go:build linux

package cni

import (
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"os"
	"regexp"

	"github.com/containernetworking/cni/pkg/skel"
	"github.com/containernetworking/cni/pkg/types"
	current "github.com/containernetworking/cni/pkg/types/100"
	"github.com/containernetworking/plugins/pkg/ipam"
	"github.com/containernetworking/plugins/pkg/ns"
	"golang.org/x/sys/unix"
	"kubenetdoctor/internal/api"
	"kubenetdoctor/internal/network"
)

func nodeConfig(c *Config) network.NodeConfig {
	return network.NodeConfig{Name: c.Name, PodCIDR: c.PodCIDR, Gateway: c.Gateway, MTU: c.MTU}
}

var containerIDPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`)
var interfaceNamePattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}$`)

func validateArgs(args *skel.CmdArgs) error {
	if len(args.ContainerID) > 1024 || !containerIDPattern.MatchString(args.ContainerID) {
		return fmt.Errorf("invalid container ID")
	}
	if !interfaceNamePattern.MatchString(args.IfName) {
		return fmt.Errorf("invalid interface name")
	}
	return nil
}

// Add 建立可恢复的网络附件；M1 standalone 只用于隔离基线实验。
func Add(args *skel.CmdArgs) (err error) {
	c, err := ParseConfig(args.StdinData)
	if err != nil {
		return err
	}
	if err = validateArgs(args); err != nil {
		return err
	}
	var pod api.PodIdentity
	if !c.Standalone {
		pod, err = podIdentity(args)
		if err != nil {
			return err
		}
	}
	podNS, err := ns.GetNS(args.Netns)
	if err != nil {
		return err
	}
	defer podNS.Close()
	inode, err := namespaceInode(args.Netns)
	if err != nil {
		return err
	}
	hostInode, err := namespaceInode("/proc/self/ns/net")
	if err != nil {
		return err
	}
	if inode == hostInode {
		return fmt.Errorf("refusing to attach the host network namespace")
	}
	s, err := openStore(c.StateDir)
	if err != nil {
		return err
	}
	defer s.close()
	key := AttachmentKey(c.Name, args.ContainerID, args.IfName)
	old, err := s.read(key)
	if err != nil {
		return err
	}
	if old != nil {
		if old.ContainerID != args.ContainerID || old.IfName != args.IfName {
			return fmt.Errorf("journal identity mismatch")
		}
		if old.Phase == "ready" {
			if !c.Standalone && old.Pod != pod {
				return fmt.Errorf("existing attachment Pod identity differs")
			}
			if old.ConfigDigest != c.Digest || old.NetnsInode != inode || old.Result == nil {
				return fmt.Errorf("existing attachment configuration differs; DEL required")
			}
			if err = check(c, old); err != nil {
				return err
			}
			return types.PrintResult(old.Result, c.CNIVersion)
		}
		if err = cleanup(s, old); err != nil {
			return fmt.Errorf("incomplete attachment recovery: %w", err)
		}
	}
	a := &Attachment{Schema: 1, Key: key, ContainerID: args.ContainerID, Phase: "validated",
		Pod:          pod,
		ConfigDigest: c.Digest, RawConfig: append([]byte(nil), c.Raw...),
		Endpoint: network.Endpoint{Owner: "knd:v1:" + key, IfName: args.IfName, Netns: args.Netns,
			NetnsInode: inode, HostName: "kn" + key[:11]}}
	mac := make(net.HardwareAddr, 6)
	if _, err = rand.Read(mac); err != nil {
		return err
	}
	mac[0] = (mac[0] | 2) & 0xfe // 本地管理、单播地址，创建前写入事务记录。
	a.HostMAC = mac.String()
	if err = s.write(a); err != nil {
		return err
	}
	defer func() {
		if err != nil {
			err = errors.Join(err, cleanup(s, a))
		}
	}()
	if err = network.PrepareNode(nodeConfig(c)); err != nil {
		return err
	}
	allocated, err := ipam.ExecAdd(c.IPAM.Type, c.Raw)
	if err != nil {
		return err
	}
	result, err := current.NewResultFromResult(allocated)
	if err != nil {
		return err
	}
	if len(result.IPs) != 1 || result.IPs[0].Address.IP.To4() == nil {
		return fmt.Errorf("IPAM must return exactly one IPv4 address")
	}
	ip := result.IPs[0].Address.IP.To4()
	_, subnet, _ := net.ParseCIDR(c.PodCIDR)
	n := binary.BigEndian.Uint32(ip)
	base := binary.BigEndian.Uint32(subnet.IP.To4())
	last := base | ^binary.BigEndian.Uint32(subnet.Mask)
	if !subnet.Contains(ip) || n <= base+1 || n >= last {
		return fmt.Errorf("IPAM returned an address reserved by the topology")
	}
	a.IP = ip.String()
	if err = advance(s, a, "allocated"); err != nil {
		return err
	}
	if err = network.CreateEndpoint(nodeConfig(c), &a.Endpoint); err != nil {
		return err
	}
	if err = advance(s, a, "links"); err != nil {
		return err
	}
	if !c.Standalone {
		reply, callErr := api.Call(c.AgentSocket, "prepare", agentRequest(a))
		if callErr != nil {
			return callErr
		}
		a.TransactionID = reply.TransactionID
		if err = advance(s, a, "prepared"); err != nil {
			return err
		}
	}
	if err = network.EnableEndpoint(a.Endpoint); err != nil {
		return err
	}
	if c.Standalone {
		if err = network.ConfigureHost(nodeConfig(c), a.Endpoint); err != nil {
			return err
		}
	} else {
		reply, callErr := api.Call(c.AgentSocket, "commit", agentRequest(a))
		if callErr != nil {
			return callErr
		}
		if !reply.Ready {
			return fmt.Errorf("Agent did not commit local readiness")
		}
	}
	one := 1
	_, defaultRoute, _ := net.ParseCIDR("0.0.0.0/0")
	a.Result = &current.Result{CNIVersion: c.CNIVersion, DNS: result.DNS,
		Interfaces: []*current.Interface{{Name: a.HostName, Mac: a.HostMAC, Mtu: c.MTU},
			{Name: a.IfName, Mac: a.PodMAC, Mtu: c.MTU, Sandbox: a.Netns}},
		IPs: []*current.IPConfig{{Interface: &one, Address: net.IPNet{IP: ip, Mask: net.CIDRMask(32, 32)}, Gateway: net.ParseIP(c.Gateway)}},
		Routes: []*types.Route{{Dst: net.IPNet{IP: net.ParseIP(c.Gateway).To4(), Mask: net.CIDRMask(32, 32)}},
			{Dst: *defaultRoute, GW: net.ParseIP(c.Gateway)}}}
	if err = advance(s, a, "ready"); err != nil {
		return err
	}
	if err = check(c, a); err != nil {
		return err
	}
	return types.PrintResult(a.Result, c.CNIVersion)
}

// Del 即使 Pod netns 已被 runtime 删除，也能凭宿主接口所有权清理。
func Del(args *skel.CmdArgs) error {
	c, err := ParseConfig(args.StdinData)
	if err != nil {
		return err
	}
	if err = validateArgs(args); err != nil {
		return err
	}
	s, err := openStore(c.StateDir)
	if err != nil {
		return err
	}
	defer s.close()
	key := AttachmentKey(c.Name, args.ContainerID, args.IfName)
	a, err := s.read(key)
	if err != nil {
		return err
	}
	if a == nil {
		ep := network.Endpoint{HostName: "kn" + key[:11], Owner: "knd:v1:" + key}
		if err = network.RemoveEndpoint(ep); err != nil {
			return err
		}
		return ipam.ExecDel(c.IPAM.Type, c.Raw)
	}
	if a.ContainerID != args.ContainerID || a.IfName != args.IfName {
		return fmt.Errorf("journal identity mismatch")
	}
	return cleanup(s, a)
}

// Check 验证原附件身份和实际网络配置，不修改期望之外的网络对象。
func Check(args *skel.CmdArgs) error {
	c, err := ParseConfig(args.StdinData)
	if err != nil {
		return err
	}
	if err = validateArgs(args); err != nil {
		return err
	}
	s, err := openStore(c.StateDir)
	if err != nil {
		return err
	}
	defer s.close()
	a, err := s.read(AttachmentKey(c.Name, args.ContainerID, args.IfName))
	if err != nil {
		return err
	}
	if a == nil || a.Phase != "ready" {
		return fmt.Errorf("attachment not ready")
	}
	if a.ConfigDigest != c.Digest || a.Netns != args.Netns {
		return fmt.Errorf("CHECK configuration differs from ADD")
	}
	return check(c, a)
}

func check(c *Config, a *Attachment) error {
	inode, err := namespaceInode(a.Netns)
	if err != nil {
		return err
	}
	if inode != a.NetnsInode {
		return fmt.Errorf("Pod namespace identity changed")
	}
	if err = network.CheckEndpoint(nodeConfig(c), a.Endpoint); err != nil {
		return err
	}
	if !c.Standalone {
		reply, err := api.Call(c.AgentSocket, "endpoint", agentRequest(a))
		if err != nil {
			return err
		}
		if !reply.Ready || reply.TransactionID != a.TransactionID {
			return fmt.Errorf("Agent attachment is not ready")
		}
	}
	return ipam.ExecCheck(c.IPAM.Type, a.RawConfig)
}

func cleanup(s *store, a *Attachment) error {
	c, err := ParseConfig(a.RawConfig)
	if err != nil {
		return err
	}
	var agentErr error
	if !c.Standalone {
		_, agentErr = api.Call(c.AgentSocket, "delete", agentRequest(a))
	}
	if err := network.RemoveEndpoint(a.Endpoint); err != nil {
		return err
	}
	// 先确认旧链路断开，才允许回收 IP；原始配置保证 dataDir 变化时仍释放旧租约。
	if err := ipam.ExecDel("host-local", a.RawConfig); err != nil {
		return err
	}
	if agentErr != nil {
		// 已断开且 IPAM 已释放；保留有界恢复记录供 Agent 重启扫描，DEL 对 runtime 成功。
		a.Phase = "detached"
		return s.write(a)
	}
	return s.remove(a.Key)
}

func advance(s *store, a *Attachment, phase string) error {
	a.Phase = phase
	if err := s.write(a); err != nil {
		return err
	}
	// 仅集成测试使用的故障注入；默认完全关闭，不通过 CNI JSON 开放。
	if os.Getenv("KND_TEST_CRASH_AFTER") == phase {
		_ = unix.Kill(os.Getpid(), unix.SIGKILL)
		select {}
	}
	if os.Getenv("KND_TEST_FAIL_AFTER") == phase {
		return fmt.Errorf("injected failure after %s", phase)
	}
	return nil
}
