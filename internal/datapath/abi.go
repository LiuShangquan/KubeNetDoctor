package datapath

import (
	"encoding/binary"
	"fmt"
	"net"
	"net/netip"

	"kubenetdoctor/internal/network"
)

const ABI = 2
const Capacity = 4096
const (
	Local   uint32 = 1
	Ready   uint32 = 2
	Blocked uint32 = 4
)
const (
	EndpointKind uint32 = 1
	NodeKind     uint32 = 2
	DenyKind     uint32 = 3
	MetaKind     uint32 = 4
)

type Key struct{ Kind, A, B, Protocol uint32 }
type Value struct {
	ID, Epoch                  uint64
	IP, IfIndex, NodeID, Flags uint32
	HostMAC, PodMAC            [6]byte
	Pad                        uint32
	Reserved                   [2]uint64
}
type Config struct {
	ABI, FastReady, Mode, IngressIfIndex       uint32
	ServiceNetwork, ServiceMask, MTU, Features uint32
}
type Endpoint struct {
	network.Endpoint
	ID    uint64 `json:"id"`
	Epoch uint64 `json:"epoch"`
	Ready bool   `json:"ready"`
}
type Deny struct {
	SourceID      uint64 `json:"sourceID"`
	DestinationID uint64 `json:"destinationID"`
	Protocol      uint32 `json:"protocol"`
}
type Snapshot struct {
	Generation     uint64           `json:"generation"`
	ServiceCIDR    string           `json:"serviceCIDR"`
	MTU            uint32           `json:"mtu"`
	FastReady      bool             `json:"fastReady"`
	Endpoints      []Endpoint       `json:"endpoints"`
	Deny           []Deny           `json:"deny,omitempty"`
	Mode           uint32           `json:"mode,omitempty"`
	IngressIfIndex uint32           `json:"ingressIfIndex,omitempty"`
	Remote         []RemoteEndpoint `json:"remote,omitempty"`
	Paths          []NodePath       `json:"paths,omitempty"`
}
type RemoteEndpoint struct {
	UID    string `json:"uid,omitempty"`
	ID     uint64 `json:"id"`
	Epoch  uint64 `json:"epoch"`
	IP     string `json:"ip"`
	NodeID uint32 `json:"nodeID"`
}
type NodePath struct {
	ID       uint32 `json:"id"`
	IP       string `json:"ip"`
	IfIndex  uint32 `json:"ifIndex"`
	LocalMAC string `json:"localMAC"`
	PeerMAC  string `json:"peerMAC"`
}
type Compiled struct {
	Config   Config
	Entries  map[Key]Value
	Bindings map[uint32]Value
}

var Reasons = []string{
	"redirect_requested", "delivery_seen", "ct_miss",
	"slow_disabled", "slow_service", "slow_ct_hit", "slow_ct_error", "slow_unknown_peer",
	"slow_neigh", "slow_mtu", "slow_ttl", "slow_options_fragment", "slow_offload",
	"slow_fib", "slow_protocol", "slow_l2", "slow_binding",
	"drop_source", "drop_endpoint", "drop_acl", "drop_parse", "drop_mutation", "delivery_fast_seen",
}

// IPWord 使 uint32 序列化后的四字节保持 IPv4 网络序，不把主机整数当网络字节。
func IPWord(ip string) (uint32, error) {
	a, err := netip.ParseAddr(ip)
	if err != nil || !a.Is4() {
		return 0, fmt.Errorf("invalid IPv4 %q", ip)
	}
	raw := a.As4()
	return binary.NativeEndian.Uint32(raw[:]), nil
}

// Compile 先完整验证容量、唯一性和 ABI，再允许内核发布；错误不会生成半张表。
func Compile(s Snapshot) (*Compiled, error) {
	if s.Generation == 0 || s.MTU < 576 || s.MTU > 9000 {
		return nil, fmt.Errorf("invalid generation/MTU")
	}
	p, err := netip.ParsePrefix(s.ServiceCIDR)
	if err != nil || !p.Addr().Is4() || p.Bits() == 0 || p != p.Masked() {
		return nil, fmt.Errorf("invalid ServiceCIDR")
	}
	networkWord, _ := IPWord(p.Addr().String())
	mask := net.CIDRMask(p.Bits(), 32)
	c := &Compiled{Config: Config{ABI: ABI, ServiceNetwork: networkWord, ServiceMask: binary.NativeEndian.Uint32(mask), MTU: s.MTU},
		Entries: map[Key]Value{{Kind: MetaKind}: {ID: s.Generation}}, Bindings: map[uint32]Value{}}
	if s.FastReady {
		c.Config.FastReady = 1
	}
	if len(s.Endpoints)+len(s.Remote) > 256 || len(s.Deny) > 256 || len(s.Paths) > 32 {
		return nil, fmt.Errorf("snapshot capacity exceeded")
	}
	if s.Mode > 2 || (s.Mode != 0 && s.IngressIfIndex == 0) {
		return nil, fmt.Errorf("invalid ingress mode/interface")
	}
	c.Config.Mode = s.Mode
	c.Config.IngressIfIndex = s.IngressIfIndex
	byID := map[uint64]Value{}
	for _, ep := range s.Endpoints {
		ip, err := IPWord(ep.IP)
		if err != nil {
			return nil, err
		}
		if ep.ID == 0 || ep.Epoch == 0 || ep.HostIndex <= 0 || ep.HostName == "" || ep.Owner == "" || p.Contains(netip.MustParseAddr(ep.IP)) {
			return nil, fmt.Errorf("invalid endpoint identity or Service overlap")
		}
		v := Value{ID: ep.ID, Epoch: ep.Epoch, IP: ip, IfIndex: uint32(ep.HostIndex), Flags: Local}
		if ep.Ready {
			v.Flags |= Ready
		} else {
			v.Flags |= Blocked
		}
		host, e1 := net.ParseMAC(ep.HostMAC)
		pod, e2 := net.ParseMAC(ep.PodMAC)
		if e1 != nil || e2 != nil || len(host) != 6 || len(pod) != 6 || host[0]&1 != 0 || pod[0]&1 != 0 {
			return nil, fmt.Errorf("invalid unicast MAC")
		}
		copy(v.HostMAC[:], host)
		copy(v.PodMAC[:], pod)
		k := Key{Kind: EndpointKind, A: ip}
		if _, ok := c.Entries[k]; ok {
			return nil, fmt.Errorf("duplicate Pod IP")
		}
		if _, ok := c.Bindings[v.IfIndex]; ok {
			return nil, fmt.Errorf("duplicate ifindex")
		}
		if _, ok := byID[v.ID]; ok {
			return nil, fmt.Errorf("duplicate endpoint ID")
		}
		c.Entries[k] = v
		c.Bindings[v.IfIndex] = v
		byID[v.ID] = v
	}
	for _, path := range s.Paths {
		ip, err := IPWord(path.IP)
		if err != nil {
			return nil, err
		}
		if path.ID == 0 || path.IfIndex == 0 || (s.Mode != 0 && path.IfIndex != s.IngressIfIndex) {
			return nil, fmt.Errorf("invalid node path")
		}
		local, e1 := net.ParseMAC(path.LocalMAC)
		peer, e2 := net.ParseMAC(path.PeerMAC)
		if e1 != nil || e2 != nil || len(local) != 6 || len(peer) != 6 || local[0]&1 != 0 || peer[0]&1 != 0 {
			return nil, fmt.Errorf("invalid node path MAC")
		}
		key := Key{Kind: NodeKind, A: path.ID}
		if _, exists := c.Entries[key]; exists {
			return nil, fmt.Errorf("duplicate node ID")
		}
		v := Value{ID: uint64(path.ID), IP: ip, IfIndex: path.IfIndex, NodeID: path.ID}
		copy(v.HostMAC[:], local)
		copy(v.PodMAC[:], peer)
		c.Entries[key] = v
	}
	for _, ep := range s.Remote {
		ip, err := IPWord(ep.IP)
		if err != nil {
			return nil, err
		}
		if ep.ID == 0 || ep.Epoch == 0 || ep.NodeID == 0 || p.Contains(netip.MustParseAddr(ep.IP)) {
			return nil, fmt.Errorf("invalid remote endpoint")
		}
		if _, exists := c.Entries[Key{Kind: NodeKind, A: ep.NodeID}]; !exists {
			return nil, fmt.Errorf("remote node path missing")
		}
		key := Key{Kind: EndpointKind, A: ip}
		if _, exists := c.Entries[key]; exists {
			return nil, fmt.Errorf("duplicate endpoint IP")
		}
		if _, exists := byID[ep.ID]; exists {
			return nil, fmt.Errorf("duplicate endpoint ID")
		}
		v := Value{ID: ep.ID, Epoch: ep.Epoch, IP: ip, NodeID: ep.NodeID, Flags: Ready}
		c.Entries[key] = v
		byID[ep.ID] = v
	}
	for _, acl := range s.Deny {
		src, a := byID[acl.SourceID]
		dst, b := byID[acl.DestinationID]
		if !a || !b || (acl.Protocol != 6 && acl.Protocol != 17) {
			return nil, fmt.Errorf("ACL requires registered endpoint IDs and TCP/UDP")
		}
		c.Entries[Key{Kind: DenyKind, A: src.IP, B: dst.IP, Protocol: acl.Protocol}] = Value{ID: s.Generation}
	}
	return c, nil
}
