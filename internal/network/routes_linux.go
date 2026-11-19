//go:build linux

package network

import (
	"fmt"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
	"net"
)

type NodeRoute struct {
	CIDR    string `json:"cidr"`
	Gateway string `json:"gateway"`
	IfIndex int    `json:"ifIndex"`
	Onlink  bool   `json:"onlink,omitempty"`
}

// ReconcileNodeRoutes 只管理持久记录中的远端聚合路由；不扫描删除其他路由。
func ReconcileNodeRoutes(desired, previous []NodeRoute) error {
	wanted := map[string]NodeRoute{}
	for _, plan := range desired {
		_, cidr, err := net.ParseCIDR(plan.CIDR)
		if err != nil {
			return err
		}
		if _, exists := wanted[plan.CIDR]; exists {
			return fmt.Errorf("duplicate node CIDR")
		}
		wanted[plan.CIDR] = plan
		routes, err := netlink.RouteListFiltered(netlink.FAMILY_V4, &netlink.Route{Dst: cidr}, netlink.RT_FILTER_DST)
		if err != nil {
			return err
		}
		for _, r := range routes {
			if r.Protocol != RouteProtocol {
				return fmt.Errorf("foreign route owns %s", plan.CIDR)
			}
		}
	}
	for _, plan := range desired {
		_, cidr, _ := net.ParseCIDR(plan.CIDR)
		flags := 0
		if plan.Onlink {
			flags = unix.RTNH_F_ONLINK
		}
		if err := netlink.RouteReplace(&netlink.Route{Dst: cidr, Gw: net.ParseIP(plan.Gateway).To4(), LinkIndex: plan.IfIndex, Protocol: RouteProtocol, Flags: flags}); err != nil {
			return err
		}
	}
	for _, old := range previous {
		if _, keep := wanted[old.CIDR]; keep {
			continue
		}
		_, cidr, err := net.ParseCIDR(old.CIDR)
		if err != nil {
			return err
		}
		routes, err := netlink.RouteListFiltered(netlink.FAMILY_V4, &netlink.Route{Dst: cidr}, netlink.RT_FILTER_DST)
		if err != nil {
			return err
		}
		for _, r := range routes {
			if r.Protocol == RouteProtocol && r.LinkIndex == old.IfIndex && r.Gw.Equal(net.ParseIP(old.Gateway)) {
				if err = netlink.RouteDel(&r); err != nil {
					return err
				}
			}
		}
	}
	return nil
}
