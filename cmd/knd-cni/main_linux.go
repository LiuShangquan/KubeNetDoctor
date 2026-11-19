//go:build linux

package main

import (
	"github.com/containernetworking/cni/pkg/skel"
	"github.com/containernetworking/cni/pkg/version"
	"kubenetdoctor/internal/cni"
	"runtime"
)

// CNI 的 netns 操作绑定当前线程；普通业务报文不经过此进程。
func main() {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	skel.PluginMainFuncs(skel.CNIFuncs{Add: cni.Add, Del: cni.Del, Check: cni.Check},
		version.PluginSupports("1.0.0"), "KubeNetDoctor CNI prototype v0.1.0")
}
