package cni

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"path/filepath"
	"regexp"

	"github.com/containernetworking/cni/pkg/types"
)

// Config 定义本项目明确支持的单网络、单 IPv4 地址接入配置。
type Config struct {
	types.NetConf
	PodCIDR     string `json:"podCIDR"`
	Gateway     string `json:"gateway,omitempty"`
	MTU         int    `json:"mtu,omitempty"`
	StateDir    string `json:"stateDir,omitempty"`
	AgentSocket string `json:"agentSocket,omitempty"`
	Standalone  bool   `json:"standalone,omitempty"`
	Raw         []byte `json:"-"`
	Digest      string `json:"-"`
}

// ParseConfig 在任何地址分配、接口创建之前验证网络配置。
func ParseConfig(data []byte) (*Config, error) {
	var c Config
	if err := json.Unmarshal(data, &c); err != nil {
		return nil, fmt.Errorf("invalid CNI JSON: %w", err)
	}
	if c.CNIVersion != "1.0.0" || !regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`).MatchString(c.Name) || c.Type != "knd-cni" {
		return nil, fmt.Errorf("expected CNI 1.0.0, nonempty name and type knd-cni")
	}
	if c.IPAM.Type != "host-local" {
		return nil, fmt.Errorf("only host-local IPAM is supported")
	}
	_, subnet, err := net.ParseCIDR(c.PodCIDR)
	if err != nil || subnet.IP.To4() == nil {
		return nil, fmt.Errorf("podCIDR must be an IPv4 subnet")
	}
	ones, _ := subnet.Mask.Size()
	if ones > 30 {
		return nil, fmt.Errorf("podCIDR must leave addresses for the gateway reservation and Pods")
	}
	if c.Gateway == "" {
		c.Gateway = "169.254.1.1"
	}
	gw := net.ParseIP(c.Gateway)
	if gw == nil || gw.To4() == nil || subnet.Contains(gw) || !gw.IsLinkLocalUnicast() {
		return nil, fmt.Errorf("gateway must be a link-local IPv4 address outside podCIDR")
	}
	if c.MTU == 0 {
		c.MTU = 1450
	}
	if c.MTU < 576 || c.MTU > 9000 {
		return nil, fmt.Errorf("mtu must be between 576 and 9000")
	}
	if c.StateDir == "" {
		c.StateDir = "/var/lib/knd/cni"
	}
	if !filepath.IsAbs(c.StateDir) || filepath.Clean(c.StateDir) == "/" {
		return nil, fmt.Errorf("stateDir must be an absolute dedicated directory")
	}
	if c.AgentSocket == "" {
		c.AgentSocket = "/run/knd/agent.sock"
	}
	var raw struct {
		IPAM struct {
			Ranges [][]struct {
				Subnet string `json:"subnet"`
			} `json:"ranges"`
		} `json:"ipam"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, err
	}
	if len(raw.IPAM.Ranges) != 1 || len(raw.IPAM.Ranges[0]) != 1 {
		return nil, fmt.Errorf("exactly one host-local IPv4 range is required")
	}
	_, ipamSubnet, err := net.ParseCIDR(raw.IPAM.Ranges[0][0].Subnet)
	if err != nil || ipamSubnet.String() != subnet.String() {
		return nil, fmt.Errorf("IPAM range must equal podCIDR")
	}
	c.PodCIDR = subnet.String()
	c.Raw = append([]byte(nil), data...)
	var normalized map[string]any
	if err := json.Unmarshal(data, &normalized); err != nil {
		return nil, err
	}
	delete(normalized, "prevResult")
	canonical, err := json.Marshal(normalized)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(canonical)
	c.Digest = hex.EncodeToString(sum[:])
	return &c, nil
}

// AttachmentKey 避免把 runtime 输入直接用作文件名或接口名。
func AttachmentKey(network, container, ifName string) string {
	sum := sha256.Sum256([]byte(network + "\x00" + container + "\x00" + ifName))
	return hex.EncodeToString(sum[:])
}
