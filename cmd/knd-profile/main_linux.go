//go:build linux

// 独立测量工具：统计开启只在本进程持有 fd 时生效，不永久修改宿主 sysctl。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"github.com/cilium/ebpf"
	"golang.org/x/sys/unix"
	"os"
	"path/filepath"
	"time"
)

func run() error {
	pins := flag.String("pins", "", "专用 pin 目录")
	duration := flag.Duration("duration", 6*time.Second, "采样窗口")
	flag.Parse()
	closer, err := ebpf.EnableStats(unix.BPF_STATS_RUN_TIME)
	if err != nil {
		return err
	}
	defer closer.Close()
	programs := map[string]*ebpf.Program{}
	before := map[string]*ebpf.ProgramStats{}
	defer func() {
		for _, p := range programs {
			p.Close()
		}
	}()
	for _, name := range []string{"knd_from_pod", "knd_to_pod", "knd_from_network"} {
		p, err := ebpf.LoadPinnedProgram(filepath.Join(*pins, name), nil)
		if err != nil {
			return err
		}
		programs[name] = p
		s, err := p.Stats()
		if err != nil {
			return err
		}
		before[name] = s
	}
	fmt.Fprintln(os.Stderr, "READY")
	time.Sleep(*duration)
	result := map[string]any{}
	for name, p := range programs {
		after, err := p.Stats()
		if err != nil {
			return err
		}
		count := after.RunCount - before[name].RunCount
		ns := after.Runtime - before[name].Runtime
		mean := float64(0)
		if count != 0 {
			mean = float64(ns.Nanoseconds()) / float64(count)
		}
		result[name] = map[string]any{"calls": count, "runtimeNS": ns.Nanoseconds(), "meanNS": mean, "recursionMisses": after.RecursionMisses - before[name].RecursionMisses}
	}
	return json.NewEncoder(os.Stdout).Encode(result)
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
