//go:build linux

package network

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"fmt"
	"net"
	"os"
	"strings"

	"github.com/containernetworking/plugins/pkg/ns"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

const RouteProtocol = 190

// NodeConfig 是接入所需的最小节点配置；路由与接口只管理明确属于项目的对象。
type NodeConfig struct {
	Name, PodCIDR, Gateway, NodeIP string
	MTU                            int
}

// Endpoint 保存真实内核接口信息，不把 ifindex 单独当成长期身份。
type Endpoint struct {
	Owner      string `json:"owner"`
	IfName     string `json:"ifName"`
	Netns      string `json:"netns"`
	NetnsInode uint64 `json:"netnsInode"`
	HostName   string `json:"hostName"`
	HostIndex  int    `json:"hostIndex"`
	HostMAC    string `json:"hostMAC"`
	PodMAC     string `json:"podMAC"`
	IP         string `json:"ip"`
}

func isMissing(err error) bool {
	var missing netlink.LinkNotFoundError
	return errors.As(err, &missing) || errors.Is(err, unix.ENODEV)
}

func hostPrefix(ip string) *net.IPNet {
	return &net.IPNet{IP: net.ParseIP(ip).To4(), Mask: net.CIDRMask(32, 32)}
}

// PrepareNode 配置共享网关和本机黑洞聚合路由；每个 Pod 删除时不拆共享资源。
func PrepareNode(c NodeConfig) error {
	forward, err := os.ReadFile("/proc/sys/net/ipv4/ip_forward")
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(forward)) != "1" {
		return fmt.Errorf("node IPv4 forwarding is disabled")
	}
	alias := "knd:v1:gateway:" + c.Name
	identity := sha256.Sum256([]byte(alias))
	gatewayMAC := net.HardwareAddr{0x02, identity[0], identity[1], identity[2], identity[3], identity[4]}
	link, err := netlink.LinkByName("knd-gw")
	if isMissing(err) {
		link = &netlink.Dummy{LinkAttrs: netlink.LinkAttrs{Name: "knd-gw", HardwareAddr: gatewayMAC, MTU: c.MTU}}
		if err = netlink.LinkAdd(link); err != nil {
			return err
		}
		link, err = netlink.LinkByName("knd-gw")
	}
	if err != nil {
		return err
	}
	// 当前内核的 NEWLINK 不保留 alias；仅凭创建时指定的完整身份恢复未完成标记。
	if link.Type() == "dummy" && link.Attrs().Alias == "" && bytes.Equal(link.Attrs().HardwareAddr, gatewayMAC) {
		if err = netlink.LinkSetAlias(link, alias); err != nil {
			return err
		}
		link, err = netlink.LinkByName("knd-gw")
		if err != nil {
			return err
		}
	}
	if link.Type() != "dummy" || link.Attrs().Alias != alias {
		return fmt.Errorf("knd-gw ownership conflict: type=%q alias=%q expected=%q", link.Type(), link.Attrs().Alias, alias)
	}
	addrs, err := netlink.AddrList(link, netlink.FAMILY_V4)
	if err != nil {
		return err
	}
	want := hostPrefix(c.Gateway)
	if len(addrs) == 0 {
		if err = netlink.AddrAdd(link, &netlink.Addr{IPNet: want}); err != nil {
			return err
		}
	} else if len(addrs) != 1 || addrs[0].IPNet.String() != want.String() {
		return fmt.Errorf("knd-gw address conflict")
	}
	if err = netlink.LinkSetUp(link); err != nil {
		return err
	}
	_, subnet, _ := net.ParseCIDR(c.PodCIDR)
	routes, err := netlink.RouteListFiltered(netlink.FAMILY_V4, &netlink.Route{Dst: subnet}, netlink.RT_FILTER_DST)
	if err != nil {
		return err
	}
	if len(routes) != 0 {
		for _, route := range routes {
			if route.Protocol != RouteProtocol || route.Type != unix.RTN_BLACKHOLE {
				return fmt.Errorf("podCIDR route ownership conflict")
			}
		}
		return nil
	}
	return netlink.RouteAdd(&netlink.Route{Dst: subnet, Type: unix.RTN_BLACKHOLE, Protocol: RouteProtocol})
}

// CreateEndpoint 直接使用 netlink 创建 DOWN 状态 veth，避免助手函数提前启用链路。
func CreateEndpoint(c NodeConfig, ep *Endpoint) error {
	podNS, err := ns.GetNS(ep.Netns)
	if err != nil {
		return err
	}
	defer podNS.Close()
	if _, err := netlink.LinkByName(ep.HostName); err == nil {
		return fmt.Errorf("host interface already exists: %s", ep.HostName)
	} else if !isMissing(err) {
		return err
	}
	if err := podNS.Do(func(_ ns.NetNS) error {
		if _, err := netlink.LinkByName(ep.IfName); err == nil {
			return fmt.Errorf("Pod interface already exists")
		} else if !isMissing(err) {
			return err
		}
		return nil
	}); err != nil {
		return err
	}
	mac, err := net.ParseMAC(ep.HostMAC)
	if err != nil {
		return err
	}
	veth := &netlink.Veth{LinkAttrs: netlink.LinkAttrs{Name: ep.HostName, HardwareAddr: mac, MTU: c.MTU},
		PeerName: ep.IfName, PeerNamespace: netlink.NsFd(podNS.Fd()), PeerMTU: uint32(c.MTU), PeerTxQLen: -1}
	if err = netlink.LinkAdd(veth); err != nil {
		return err
	}
	if os.Getenv("KND_TEST_CRASH_AFTER") == "created-host" {
		_ = unix.Kill(os.Getpid(), unix.SIGKILL)
		select {}
	}
	host, err := netlink.LinkByName(ep.HostName)
	if err != nil {
		return err
	}
	if err = netlink.LinkSetAlias(host, ep.Owner); err != nil {
		return err
	}
	ep.HostIndex, ep.HostMAC = host.Attrs().Index, host.Attrs().HardwareAddr.String()
	if err = podNS.Do(func(_ ns.NetNS) error {
		peer, err := netlink.LinkByName(ep.IfName)
		if err != nil {
			return err
		}
		ep.PodMAC = peer.Attrs().HardwareAddr.String()
		if err = netlink.LinkSetAlias(peer, ep.Owner+":pod"); err != nil {
			return err
		}
		if err = netlink.AddrAdd(peer, &netlink.Addr{IPNet: hostPrefix(ep.IP)}); err != nil {
			return err
		}
		// IPv4 路由配置要求接口 UP；宿主端仍 DOWN，附件尚不能收发。
		if err = netlink.LinkSetUp(peer); err != nil {
			return err
		}
		if err = netlink.RouteAdd(&netlink.Route{LinkIndex: peer.Attrs().Index, Dst: hostPrefix(c.Gateway),
			Scope: netlink.SCOPE_LINK, Protocol: RouteProtocol}); err != nil {
			return fmt.Errorf("Pod gateway route: %w", err)
		}
		if err = netlink.RouteAdd(&netlink.Route{LinkIndex: peer.Attrs().Index, Gw: net.ParseIP(c.Gateway).To4(),
			Protocol: RouteProtocol}); err != nil {
			return fmt.Errorf("Pod default route: %w", err)
		}
		return netlink.NeighSet(&netlink.Neigh{LinkIndex: peer.Attrs().Index, Family: unix.AF_INET,
			IP: net.ParseIP(c.Gateway).To4(), HardwareAddr: host.Attrs().HardwareAddr, State: netlink.NUD_PERMANENT})
	}); err != nil {
		return err
	}
	return nil
}

// ConfigureHost 把 Pod /32 路由和永久邻居安装到节点；后续由 Agent 复用。
func ConfigureHost(c NodeConfig, ep Endpoint) error {
	link, err := ownedLink(ep)
	if err != nil {
		return err
	}
	routes, err := netlink.RouteListFiltered(netlink.FAMILY_V4, &netlink.Route{Dst: hostPrefix(ep.IP)}, netlink.RT_FILTER_DST)
	if err != nil {
		return err
	}
	for _, r := range routes {
		if r.LinkIndex != link.Attrs().Index || r.Protocol != RouteProtocol {
			return fmt.Errorf("Pod /32 route ownership conflict")
		}
	}
	route := &netlink.Route{LinkIndex: link.Attrs().Index, Dst: hostPrefix(ep.IP), Scope: netlink.SCOPE_LINK, Protocol: RouteProtocol}
	if c.NodeIP != "" {
		route.Src = net.ParseIP(c.NodeIP).To4()
	}
	if len(routes) == 0 {
		if err = netlink.RouteAdd(route); err != nil {
			return err
		}
	}
	mac, err := net.ParseMAC(ep.PodMAC)
	if err != nil {
		return err
	}
	return netlink.NeighSet(&netlink.Neigh{LinkIndex: link.Attrs().Index, Family: unix.AF_INET,
		IP: net.ParseIP(ep.IP).To4(), HardwareAddr: mac, State: netlink.NUD_PERMANENT})
}

func ownedLink(ep Endpoint) (netlink.Link, error) {
	link, err := netlink.LinkByName(ep.HostName)
	if err != nil {
		return nil, err
	}
	aliasMatches := link.Attrs().Alias == ep.Owner
	// MAC 在创建前已持久化，允许清理创建成功但尚未来得及设置 alias 的附件。
	if link.Attrs().Alias == "" && ep.HostMAC != "" && link.Attrs().HardwareAddr.String() == ep.HostMAC {
		aliasMatches = true
	}
	if link.Type() != "veth" || !aliasMatches || (ep.HostMAC != "" && link.Attrs().HardwareAddr.String() != ep.HostMAC) {
		return nil, fmt.Errorf("interface ownership conflict: %s", ep.HostName)
	}
	if ep.HostIndex != 0 && ep.HostIndex != link.Attrs().Index {
		return nil, fmt.Errorf("interface index changed: %s", ep.HostName)
	}
	return link, nil
}

// EnableEndpoint 只能在配置、登记与门禁就绪后调用。
func EnableEndpoint(ep Endpoint) error {
	host, err := ownedLink(ep)
	if err != nil {
		return err
	}
	podNS, err := ns.GetNS(ep.Netns)
	if err != nil {
		return err
	}
	defer podNS.Close()
	if err = podNS.Do(func(_ ns.NetNS) error {
		peer, err := netlink.LinkByName(ep.IfName)
		if err != nil {
			return err
		}
		if peer.Attrs().Alias != ep.Owner+":pod" {
			return fmt.Errorf("Pod interface ownership conflict")
		}
		lo, err := netlink.LinkByName("lo")
		if err != nil {
			return err
		}
		if err = netlink.LinkSetUp(lo); err != nil {
			return err
		}
		return netlink.LinkSetUp(peer)
	}); err != nil {
		return err
	}
	return netlink.LinkSetUp(host)
}

// RemoveEndpoint 只删除匹配 alias/类型/编号的宿主 veth；内核同时删除其 peer、邻居和设备路由。
func RemoveEndpoint(ep Endpoint) error {
	link, err := ownedLink(ep)
	if isMissing(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if err = netlink.LinkDel(link); err != nil {
		return err
	}
	if _, err = netlink.LinkByName(ep.HostName); !isMissing(err) {
		return fmt.Errorf("interface deletion not confirmed: %s", ep.HostName)
	}
	return nil
}

// CheckEndpoint 只读取实际内核状态；不会用 CHECK 悄悄修复配置漂移。
func CheckEndpoint(c NodeConfig, ep Endpoint) error {
	host, err := ownedLink(ep)
	if err != nil {
		return err
	}
	if host.Attrs().Flags&net.FlagUp == 0 || host.Attrs().MTU != c.MTU || host.Attrs().HardwareAddr.String() != ep.HostMAC {
		return fmt.Errorf("host interface state differs")
	}
	if err = checkRoute(host.Attrs().Index, hostPrefix(ep.IP), nil); err != nil {
		return err
	}
	if err = checkNeighbor(host.Attrs().Index, ep.IP, ep.PodMAC); err != nil {
		return err
	}
	podNS, err := ns.GetNS(ep.Netns)
	if err != nil {
		return err
	}
	defer podNS.Close()
	return podNS.Do(func(_ ns.NetNS) error {
		peer, err := netlink.LinkByName(ep.IfName)
		if err != nil {
			return err
		}
		if peer.Type() != "veth" || peer.Attrs().Alias != ep.Owner+":pod" || peer.Attrs().Flags&net.FlagUp == 0 ||
			peer.Attrs().MTU != c.MTU || peer.Attrs().HardwareAddr.String() != ep.PodMAC {
			return fmt.Errorf("Pod interface state differs")
		}
		addrs, err := netlink.AddrList(peer, netlink.FAMILY_V4)
		if err != nil {
			return err
		}
		found := false
		for _, addr := range addrs {
			if addr.IPNet.String() == hostPrefix(ep.IP).String() {
				found = true
			}
		}
		if !found {
			return fmt.Errorf("Pod /32 address missing")
		}
		if err = checkRoute(peer.Attrs().Index, hostPrefix(c.Gateway), nil); err != nil {
			return err
		}
		if err = checkRoute(peer.Attrs().Index, nil, net.ParseIP(c.Gateway)); err != nil {
			return err
		}
		return checkNeighbor(peer.Attrs().Index, c.Gateway, ep.HostMAC)
	})
}

func checkRoute(index int, dst *net.IPNet, gw net.IP) error {
	routes, err := netlink.RouteListFiltered(netlink.FAMILY_V4, &netlink.Route{LinkIndex: index}, netlink.RT_FILTER_OIF)
	if err != nil {
		return err
	}
	for _, r := range routes {
		dstMatches := (dst == nil && (r.Dst == nil || r.Dst.String() == "0.0.0.0/0")) || (dst != nil && r.Dst != nil && dst.String() == r.Dst.String())
		gwMatches := (gw == nil && r.Gw == nil) || (gw != nil && r.Gw.Equal(gw))
		if dstMatches && gwMatches && r.Protocol == RouteProtocol {
			return nil
		}
	}
	return fmt.Errorf("expected owned route dst=%v gw=%v missing on ifindex %d; routes=%v", dst, gw, index, routes)
}

func checkNeighbor(index int, ip, mac string) error {
	entries, err := netlink.NeighList(index, netlink.FAMILY_V4)
	if err != nil {
		return err
	}
	expected, err := net.ParseMAC(mac)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IP.Equal(net.ParseIP(ip)) && bytes.Equal(entry.HardwareAddr, expected) && entry.State&netlink.NUD_PERMANENT != 0 {
			return nil
		}
	}
	return fmt.Errorf("expected permanent neighbor missing on ifindex %d", index)
}
