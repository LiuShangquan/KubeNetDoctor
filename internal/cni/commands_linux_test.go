//go:build linux

package cni

import (
	"github.com/containernetworking/cni/pkg/skel"
	"strings"
	"testing"
)

func TestArgumentBoundaries(t *testing.T) {
	for _, tc := range []struct {
		id, iface string
		valid     bool
	}{
		{"sandbox-abc", "eth0", true}, {strings.Repeat("a", 1024), "eth0", true},
		{strings.Repeat("a", 1025), "eth0", false}, {"../sandbox", "eth0", false},
		{"", "eth0", false}, {"sandbox", strings.Repeat("i", 16), false},
	} {
		err := validateArgs(&skel.CmdArgs{ContainerID: tc.id, IfName: tc.iface})
		if (err == nil) != tc.valid {
			t.Errorf("id length=%d iface=%q: %v", len(tc.id), tc.iface, err)
		}
	}
}
