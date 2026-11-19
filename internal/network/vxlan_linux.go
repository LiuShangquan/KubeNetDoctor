//go:build linux

package network

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
	"net"
)

type VXLANPeer struct {
	VTEP   string `json:"vtep"`
	MAC    string `json:"mac"`
	NodeIP string `json:"nodeIP"`
}

// VTEPMAC 使用 Node UID 产生稳定、本地管理的单播 MAC；调用者另做冲突检查。
func VTEPMAC(uid string) net.HardwareAddr {
	sum := sha256.Sum256([]byte("knd-vxlan:" + uid))
	return net.HardwareAddr{0x02, sum[0], sum[1], sum[2], sum[3], sum[4]}
}

// RemoveVXLAN 在模式受控切换、旧路由已撤销后删除本节点专用隧道。
func RemoveVXLAN(uid string) error {
	link, err := netlink.LinkByName("kndvx0")
	if isMissing(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if link.Type() != "vxlan" || link.Attrs().Alias != "knd:v2:vxlan:"+uid || !bytes.Equal(link.Attrs().HardwareAddr, VTEPMAC(uid)) {
		return fmt.Errorf("refuse to remove foreign VXLAN device")
	}
	return netlink.LinkDel(link)
}

// PrepareVXLAN 配置专用三层隧道设备，无 bridge、无 flood、无动态学习。
func PrepareVXLAN(underlay netlink.Link, uid, nodeIP, vtep string, mtu int) (netlink.Link, error) {
	if mtu+50 > underlay.Attrs().MTU {
		return nil, fmt.Errorf("VXLAN IPv4 outer requires MTU headroom 50")
	}
	alias := "knd:v2:vxlan:" + uid
	mac := VTEPMAC(uid)
	link, err := netlink.LinkByName("kndvx0")
	if isMissing(err) {
		vx := &netlink.Vxlan{LinkAttrs: netlink.LinkAttrs{Name: "kndvx0", MTU: mtu, HardwareAddr: mac}, VxlanId: 42, VtepDevIndex: underlay.Attrs().Index, SrcAddr: net.ParseIP(nodeIP).To4(), Port: 4789, Learning: false}
		if err = netlink.LinkAdd(vx); err != nil {
			return nil, err
		}
		link, err = netlink.LinkByName("kndvx0")
	}
	if err != nil {
		return nil, err
	}
	vx, ok := link.(*netlink.Vxlan)
	if !ok || !bytes.Equal(link.Attrs().HardwareAddr, mac) || vx.VxlanId != 42 || vx.Port != 4789 || vx.VtepDevIndex != underlay.Attrs().Index || !vx.SrcAddr.Equal(net.ParseIP(nodeIP)) || vx.Learning || link.Attrs().MTU != mtu {
		return nil, fmt.Errorf("VXLAN identity/configuration conflict")
	}
	if link.Attrs().Alias == "" {
		if err = netlink.LinkSetAlias(link, alias); err != nil {
			return nil, err
		}
		link, err = netlink.LinkByName("kndvx0")
		if err != nil {
			return nil, err
		}
	}
	if link.Attrs().Alias != alias {
		return nil, fmt.Errorf("VXLAN owner conflict")
	}
	addresses, err := netlink.AddrList(link, netlink.FAMILY_V4)
	if err != nil {
		return nil, err
	}
	if len(addresses) == 0 {
		if err = netlink.AddrAdd(link, &netlink.Addr{IPNet: hostPrefix(vtep)}); err != nil {
			return nil, err
		}
	} else if len(addresses) != 1 || addresses[0].IPNet.String() != hostPrefix(vtep).String() {
		return nil, fmt.Errorf("VTEP address conflict")
	}
	if err = netlink.LinkSetUp(link); err != nil {
		return nil, err
	}
	return link, nil
}

// EnsureVXLANPeers 先完成静态邻居/FDB，再由调用者发布 PodCIDR 路由。
// 对象仅位于已验证归属的专用设备；同键不同值拒绝覆盖。
func EnsureVXLANPeers(link netlink.Link, peers []VXLANPeer) error {
	seen := map[string]bool{}
	for _, p := range peers {
		mac, err := net.ParseMAC(p.MAC)
		if err != nil || len(mac) != 6 || net.ParseIP(p.VTEP).To4() == nil || net.ParseIP(p.NodeIP).To4() == nil {
			return fmt.Errorf("invalid VXLAN peer")
		}
		if seen[p.MAC] || seen[p.VTEP] {
			return fmt.Errorf("duplicate VTEP identity")
		}
		seen[p.MAC] = true
		seen[p.VTEP] = true
		neighbors, err := netlink.NeighList(link.Attrs().Index, netlink.FAMILY_V4)
		if err != nil {
			return err
		}
		for _, n := range neighbors {
			if n.IP.Equal(net.ParseIP(p.VTEP)) && !bytes.Equal(n.HardwareAddr, mac) {
				return fmt.Errorf("VTEP neighbor conflict")
			}
		}
		fdb, err := netlink.NeighList(link.Attrs().Index, unix.AF_BRIDGE)
		if err != nil {
			return err
		}
		for _, n := range fdb {
			if bytes.Equal(n.HardwareAddr, mac) && !n.IP.Equal(net.ParseIP(p.NodeIP)) {
				return fmt.Errorf("VTEP FDB conflict")
			}
		}
		if err = netlink.NeighSet(&netlink.Neigh{LinkIndex: link.Attrs().Index, IP: net.ParseIP(p.VTEP).To4(), HardwareAddr: mac, State: netlink.NUD_PERMANENT}); err != nil {
			return err
		}
		if err = netlink.NeighSet(&netlink.Neigh{LinkIndex: link.Attrs().Index, Family: unix.AF_BRIDGE, IP: net.ParseIP(p.NodeIP).To4(), HardwareAddr: mac, State: netlink.NUD_PERMANENT, Flags: netlink.NTF_SELF}); err != nil {
			return err
		}
	}
	return nil
}

// PruneVXLANPeers 在旧路由撤销后精确删除原持久记录，不遍历清空整个 FDB。
func PruneVXLANPeers(link netlink.Link, desired, previous []VXLANPeer) error {
	wanted := map[VXLANPeer]bool{}
	for _, p := range desired {
		wanted[p] = true
	}
	for _, p := range previous {
		if wanted[p] {
			continue
		}
		mac, err := net.ParseMAC(p.MAC)
		if err != nil {
			return err
		}
		for _, family := range []int{netlink.FAMILY_V4, unix.AF_BRIDGE} {
			list, err := netlink.NeighList(link.Attrs().Index, family)
			if err != nil {
				return err
			}
			ip := net.ParseIP(p.VTEP)
			if family == unix.AF_BRIDGE {
				ip = net.ParseIP(p.NodeIP)
			}
			for _, n := range list {
				if n.IP.Equal(ip) && bytes.Equal(n.HardwareAddr, mac) {
					if err = netlink.NeighDel(&n); err != nil {
						return err
					}
				}
			}
		}
	}
	return nil
}
