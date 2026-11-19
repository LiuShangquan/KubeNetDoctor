//go:build linux

package datapath

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/rlimit"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

var mapNames = []string{"active_snapshot", "bindings", "runtime_config", "reason_counters"}
var programNames = []string{"knd_from_pod", "knd_to_pod", "knd_from_network"}

type Manager struct {
	Dir             string
	maps            map[string]*ebpf.Map
	programs        map[string]*ebpf.Program
	fastCapable     bool
	capabilityError string
}

func (m *Manager) FastCapable() bool       { return m.fastCapable }
func (m *Manager) CapabilityError() string { return m.capabilityError }

// Open 复用固定 ABI 的 pinned maps/programs；只在全新目录加载，部分残留显式报错。
func Open(dir, object string) (m *Manager, err error) {
	if !filepath.IsAbs(dir) || filepath.Clean(dir) == "/" {
		return nil, fmt.Errorf("dedicated absolute pin directory required")
	}
	_ = rlimit.RemoveMemlock()
	if err = os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	var fs unix.Statfs_t
	if err = unix.Statfs(dir, &fs); err != nil || fs.Type != unix.BPF_FS_MAGIC {
		return nil, fmt.Errorf("pin directory must be on bpffs")
	}
	m = &Manager{Dir: dir, maps: map[string]*ebpf.Map{}, programs: map[string]*ebpf.Program{}}
	opened := m
	defer func() {
		if err != nil {
			opened.Close()
		}
	}()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	if len(entries) != 0 {
		for _, name := range mapNames {
			m.maps[name], err = ebpf.LoadPinnedMap(filepath.Join(dir, name), nil)
			if err != nil {
				return nil, err
			}
		}
		for _, name := range programNames {
			m.programs[name], err = ebpf.LoadPinnedProgram(filepath.Join(dir, name), nil)
			if err != nil {
				return nil, err
			}
		}
		var cfg Config
		if err = m.maps["runtime_config"].Lookup(uint32(0), &cfg); err != nil {
			return nil, err
		}
		if cfg.ABI != ABI {
			return nil, fmt.Errorf("pinned data plane ABI mismatch")
		}
		m.fastCapable = cfg.Features&1 != 0
		if !m.fastCapable {
			m.capabilityError = "pinned baseline program; CT acceleration unavailable"
		}
		return m, nil
	}
	spec, err := ebpf.LoadCollectionSpec(object)
	if err != nil {
		return nil, err
	}
	coll, err := ebpf.NewCollection(spec)
	if err != nil {
		// kfunc 能力不足时只加载无 CT 引用、强制内核路径的基础门禁。
		message := strings.ToLower(err.Error())
		if !strings.Contains(message, "kfunc") && !strings.Contains(message, "kernel btf") {
			return nil, fmt.Errorf("load BPF: %w", err)
		}
		m.capabilityError = err.Error()
		baseline := strings.TrimSuffix(object, ".o") + ".baseline.o"
		spec, err = ebpf.LoadCollectionSpec(baseline)
		if err != nil {
			return nil, fmt.Errorf("baseline load: %w", err)
		}
		coll, err = ebpf.NewCollection(spec)
		if err != nil {
			return nil, fmt.Errorf("baseline verifier: %w", err)
		}
	} else {
		m.fastCapable = true
	}
	defer coll.Close()
	for _, name := range mapNames {
		mp := coll.DetachMap(name)
		if mp == nil {
			return nil, fmt.Errorf("map missing: %s", name)
		}
		m.maps[name] = mp
		if err = mp.Pin(filepath.Join(dir, name)); err != nil {
			return nil, err
		}
	}
	for _, name := range programNames {
		prog := coll.DetachProgram(name)
		if prog == nil {
			return nil, fmt.Errorf("program missing: %s", name)
		}
		m.programs[name] = prog
		if err = prog.Pin(filepath.Join(dir, name)); err != nil {
			return nil, err
		}
	}
	initial := Config{ABI: ABI}
	if m.fastCapable {
		initial.Features = 1
	}
	err = m.maps["runtime_config"].Put(uint32(0), initial)
	return m, err
}
func (m *Manager) Close() {
	if m == nil {
		return
	}
	for _, mp := range m.maps {
		mp.Close()
	}
	for _, p := range m.programs {
		p.Close()
	}
}

// Apply 关闭加速，阻断发生身份变化的接口，切换不可变快照，最后开放已就绪附件。
func (m *Manager) Apply(s Snapshot) error {
	c, err := Compile(s)
	if err != nil {
		return err
	}
	if m.fastCapable {
		c.Config.Features = 1
	} else {
		c.Config.FastReady = 0
	}
	// 对所有目标先做只读所有权检查；输入错误不得触碰 live maps。
	for _, ep := range s.Endpoints {
		link, e := netlink.LinkByName(ep.HostName)
		if e != nil {
			return e
		}
		if link.Type() != "veth" || link.Attrs().Alias != ep.Owner || link.Attrs().Index != ep.HostIndex || link.Attrs().HardwareAddr.String() != ep.HostMAC {
			return fmt.Errorf("endpoint ownership mismatch: %s", ep.HostName)
		}
	}
	for _, path := range s.Paths {
		link, e := netlink.LinkByIndex(int(path.IfIndex))
		if e != nil {
			return e
		}
		if link.Attrs().HardwareAddr.String() != path.LocalMAC {
			return fmt.Errorf("node path interface MAC differs")
		}
	}
	inner, err := ebpf.NewMap(&ebpf.MapSpec{Name: "knd_snapshot", Type: ebpf.Hash, KeySize: 16, ValueSize: 64, MaxEntries: Capacity})
	if err != nil {
		return err
	}
	defer inner.Close()
	for k, v := range c.Entries {
		if err = inner.Put(k, v); err != nil {
			return err
		}
	}
	if err = inner.Freeze(); err != nil {
		return err
	}
	disabled := c.Config
	disabled.FastReady = 0
	if err = m.maps["runtime_config"].Put(uint32(0), disabled); err != nil {
		return err
	}
	current := map[uint32]Value{}
	iter := m.maps["bindings"].Iterate()
	var index uint32
	var value Value
	for iter.Next(&index, &value) {
		current[index] = value
	}
	if err = iter.Err(); err != nil {
		return err
	}
	for index, old := range current {
		want, exists := c.Bindings[index]
		if !exists || old != want {
			old.Flags = Local | Blocked
			if err = m.maps["bindings"].Put(index, old); err != nil {
				return err
			}
			if !exists {
				if _, e := netlink.LinkByIndex(int(index)); e != nil {
					var missing netlink.LinkNotFoundError
					if errors.As(e, &missing) {
						_ = m.maps["bindings"].Delete(index)
					}
				}
			}
		}
	}
	for index, v := range c.Bindings {
		if old, ok := current[index]; !ok || old != v {
			blocked := v
			blocked.Flags = Local | Blocked
			if err = m.maps["bindings"].Put(index, blocked); err != nil {
				return err
			}
		}
	}
	for _, ep := range s.Endpoints {
		if err = m.attach(ep.HostIndex, false); err != nil {
			return err
		}
	}
	if s.Mode != 0 {
		if err = m.attach(int(s.IngressIfIndex), true); err != nil {
			return err
		}
	}
	if err = m.maps["active_snapshot"].Put(uint32(0), inner); err != nil {
		return err
	}
	for index, v := range c.Bindings {
		if err = m.maps["bindings"].Put(index, v); err != nil {
			return err
		}
	}
	return m.maps["runtime_config"].Put(uint32(0), c.Config)
}

func (m *Manager) attach(index int, network bool) error {
	qdisc := &netlink.GenericQdisc{QdiscAttrs: netlink.QdiscAttrs{LinkIndex: index, Handle: netlink.MakeHandle(0xffff, 0), Parent: netlink.HANDLE_CLSACT}, QdiscType: "clsact"}
	if err := netlink.QdiscAdd(qdisc); err != nil && !errors.Is(err, unix.EEXIST) {
		return err
	}
	hooks := []struct {
		parent  uint32
		program string
	}{{netlink.HANDLE_MIN_INGRESS, "knd_from_pod"}, {netlink.HANDLE_MIN_EGRESS, "knd_to_pod"}}
	if network {
		hooks = hooks[:1]
		hooks[0].program = "knd_from_network"
	}
	for _, hook := range hooks {
		prog := m.programs[hook.program]
		info, err := prog.Info()
		if err != nil {
			return err
		}
		id, _ := info.ID()
		filters, err := netlink.FilterList(&netlink.Device{LinkAttrs: netlink.LinkAttrs{Index: index}}, hook.parent)
		if err != nil {
			return err
		}
		present := false
		for _, f := range filters {
			if f.Attrs().Priority != 49152 {
				continue
			}
			bf, ok := f.(*netlink.BpfFilter)
			if !ok || bf.Handle != netlink.MakeHandle(0, 1) || !strings.HasPrefix(bf.Name, "knd:") || bf.Id != int(id) {
				return fmt.Errorf("TC slot ownership conflict on %d", index)
			}
			present = true
		}
		if present {
			continue
		}
		f := &netlink.BpfFilter{FilterAttrs: netlink.FilterAttrs{LinkIndex: index, Parent: hook.parent, Priority: 49152, Handle: netlink.MakeHandle(0, 1), Protocol: unix.ETH_P_ALL},
			Fd: prog.FD(), Name: "knd:" + hook.program, DirectAction: true}
		if err = netlink.FilterAdd(f); err != nil {
			return err
		}
	}
	return nil
}

// BlockBinding 只修改仍匹配的旧身份，立即阻断并回读；不修改可疑网卡本身。
func (m *Manager) BlockBinding(index uint32, id, epoch uint64) error {
	var old Value
	if err := m.maps["bindings"].Lookup(index, &old); err != nil {
		return err
	}
	if old.ID != id || old.Epoch != epoch {
		return fmt.Errorf("binding identity changed before block")
	}
	old.Flags = Local | Blocked
	if err := m.maps["bindings"].Put(index, old); err != nil {
		return err
	}
	var actual Value
	if err := m.maps["bindings"].Lookup(index, &actual); err != nil {
		return err
	}
	if actual != old {
		return fmt.Errorf("binding block readback differs")
	}
	return nil
}

func (m *Manager) Stats() (map[string]uint64, error) {
	result := map[string]uint64{}
	for i, name := range Reasons {
		var perCPU []uint64
		if err := m.maps["reason_counters"].Lookup(uint32(i), &perCPU); err != nil {
			return nil, err
		}
		for _, n := range perCPU {
			result[name] += n
		}
	}
	return result, nil
}
