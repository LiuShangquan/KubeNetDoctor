package agent

import "testing"

func TestUIDReuseDoesNotInheritPolicy(t *testing.T) {
	p := Policy{Rules: []Rule{{"old-source", "destination", 17}}, BlockedUIDs: []string{"old-source"}}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	if len(p.Resolve(map[string]uint64{"old-source": 1, "destination": 2})) != 1 {
		t.Fatal("live rule missing")
	}
	if len(p.Resolve(map[string]uint64{"new-source": 1, "destination": 2})) != 0 || p.Blocked("new-source") {
		t.Fatal("replacement inherited old identity policy")
	}
}
func TestPolicyRejectsUnsupportedAndAmbiguousRules(t *testing.T) {
	for _, p := range []Policy{
		{Rules: []Rule{{"a", "b", 1}}}, {Rules: []Rule{{"", "b", 6}}},
		{Rules: []Rule{{"a", "b", 6}, {"a", "b", 6}}}, {BlockedUIDs: []string{"a", "a"}},
	} {
		if p.Validate() == nil {
			t.Fatalf("accepted invalid policy: %+v", p)
		}
	}
}
