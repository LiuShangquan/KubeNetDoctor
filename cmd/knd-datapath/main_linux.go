//go:build linux

// 独立加载/诊断入口，供 netns 实验使用；Kubernetes 的同一加载逻辑由 Agent 调用。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"kubenetdoctor/internal/datapath"
	"os"
)

func main() {
	pins := flag.String("pins", "/sys/fs/bpf/knd", "专用 bpffs 目录")
	object := flag.String("object", "build/bpf/datapath.o", "BPF ELF")
	snapshot := flag.String("apply", "", "完整快照 JSON 文件")
	stats := flag.Bool("stats", false, "读取原因计数")
	flag.Parse()
	if err := run(*pins, *object, *snapshot, *stats); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func run(pins, object, path string, stats bool) error {
	var snapshot datapath.Snapshot
	if path != "" {
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		defer f.Close()
		d := json.NewDecoder(f)
		d.DisallowUnknownFields()
		if err = d.Decode(&snapshot); err != nil {
			return err
		}
		if _, err = datapath.Compile(snapshot); err != nil {
			return err
		}
	}
	m, err := datapath.Open(pins, object)
	if err != nil {
		return err
	}
	defer m.Close()
	if path != "" {
		if err = m.Apply(snapshot); err != nil {
			return err
		}
	}
	if stats {
		r, err := m.Stats()
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(r)
	}
	return nil
}
