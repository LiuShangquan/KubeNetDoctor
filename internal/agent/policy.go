package agent

import (
	"fmt"
	"kubenetdoctor/internal/datapath"
	"strings"
)

// Policy 是附属的无状态 Pod 对限制，不宣称实现 Kubernetes NetworkPolicy。
// UID 消失后规则休眠，IP 被另一个 UID 使用时不会继承旧规则。
type Policy struct {
	APIVersion  string   `json:"apiVersion,omitempty"`
	Rules       []Rule   `json:"rules"`
	BlockedUIDs []string `json:"blockedUIDs"`
}
type Rule struct {
	SourceUID      string `json:"sourceUID"`
	DestinationUID string `json:"destinationUID"`
	Protocol       uint32 `json:"protocol"`
}

func validUID(uid string) bool {
	return len(uid) > 0 && len(uid) <= 128 && !strings.ContainsAny(uid, " \t\n\r/\\")
}
func (p Policy) Validate() error {
	if p.APIVersion != "" && p.APIVersion != "v1" {
		return fmt.Errorf("unsupported policy apiVersion")
	}
	if len(p.Rules) > 256 || len(p.BlockedUIDs) > 256 {
		return fmt.Errorf("policy capacity exceeded")
	}
	seen := map[Rule]bool{}
	for _, r := range p.Rules {
		if !validUID(r.SourceUID) || !validUID(r.DestinationUID) || (r.Protocol != 6 && r.Protocol != 17) {
			return fmt.Errorf("rule requires two Pod UIDs and TCP(6)/UDP(17)")
		}
		if seen[r] {
			return fmt.Errorf("duplicate rule")
		}
		seen[r] = true
	}
	blocked := map[string]bool{}
	for _, uid := range p.BlockedUIDs {
		if !validUID(uid) || blocked[uid] {
			return fmt.Errorf("invalid/duplicate blocked UID")
		}
		blocked[uid] = true
	}
	return nil
}
func (p Policy) Blocked(uid string) bool {
	for _, v := range p.BlockedUIDs {
		if v == uid {
			return true
		}
	}
	return false
}

// Resolve 只将当前 UID 绑定编译成规则，身份缺失时保留意图而不匹配新附件。
func (p Policy) Resolve(ids map[string]uint64) []datapath.Deny {
	var result []datapath.Deny
	for _, r := range p.Rules {
		s, d := ids[r.SourceUID], ids[r.DestinationUID]
		if s != 0 && d != 0 {
			result = append(result, datapath.Deny{SourceID: s, DestinationID: d, Protocol: r.Protocol})
		}
	}
	return result
}
