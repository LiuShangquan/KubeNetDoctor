package cni

import (
	"encoding/json"
	"testing"
)

func validConfig(t *testing.T) map[string]any {
	t.Helper()
	return map[string]any{"cniVersion": "1.0.0", "name": "knd-test", "type": "knd-cni", "podCIDR": "10.244.1.0/24",
		"standalone": true, "ipam": map[string]any{"type": "host-local", "ranges": [][]map[string]string{{{"subnet": "10.244.1.0/24", "rangeStart": "10.244.1.2"}}}}}
}

func TestRejectUnsupportedBeforeMutation(t *testing.T) {
	for _, test := range []struct {
		name  string
		key   string
		value any
	}{
		{"traversal", "name", "../escape"}, {"ipv6", "podCIDR", "fd00::/64"},
		{"small-mtu", "mtu", 100}, {"gateway-in-pool", "gateway", "10.244.1.1"},
		{"root-state", "stateDir", "/"}, {"version", "cniVersion", "1.1.0"},
	} {
		t.Run(test.name, func(t *testing.T) {
			m := validConfig(t)
			m[test.key] = test.value
			raw, _ := json.Marshal(m)
			if _, err := ParseConfig(raw); err == nil {
				t.Fatal("accepted invalid configuration")
			}
		})
	}
}

func TestDigestIgnoresPrevResult(t *testing.T) {
	m := validConfig(t)
	raw, _ := json.Marshal(m)
	a, err := ParseConfig(raw)
	if err != nil {
		t.Fatal(err)
	}
	m["prevResult"] = map[string]any{"ips": []string{"ignored"}}
	raw, _ = json.Marshal(m)
	b, err := ParseConfig(raw)
	if err != nil || a.Digest != b.Digest {
		t.Fatalf("CHECK digest mismatch: %v", err)
	}
	m["mtu"] = 1400
	raw, _ = json.Marshal(m)
	c, err := ParseConfig(raw)
	if err != nil || c.Digest == a.Digest {
		t.Fatal("network change was not detected")
	}
}

func TestAttachmentKeySeparatesFields(t *testing.T) {
	if AttachmentKey("ab", "c", "eth0") == AttachmentKey("a", "bc", "eth0") {
		t.Fatal("ambiguous identity")
	}
	if len(AttachmentKey("knd", "pod", "eth0")) != 64 {
		t.Fatal("invalid key length")
	}
}
