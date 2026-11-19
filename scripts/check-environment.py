#!/usr/bin/env python3
"""在独立 mount/net namespace 中验证项目依赖，退出时回收自建资源。"""
import datetime
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "build" / "environment"
REPORT.mkdir(parents=True, exist_ok=True)
RESULT = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "kernel": platform.release(), "checks": {}, "commands": []}


def run(args, *, check=True, timeout=45):
    """保留命令和实际输出；失败不能被汇报为通过。"""
    p = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=timeout)
    RESULT["commands"].append({"argv": [str(x) for x in args], "exit": p.returncode,
                               "stdout": p.stdout, "stderr": p.stderr})
    if check and p.returncode:
        raise RuntimeError(f"command failed: {args}\n{p.stdout}\n{p.stderr}")
    return p


def ns(name, *args, **kwargs):
    return run(["ip", "netns", "exec", name, *args], **kwargs)


def record(name, detail):
    RESULT["checks"][name] = {"status": "passed", "detail": detail}
    print(f"PASS {name}: {detail}", flush=True)


def verify():
    if os.geteuid() != 0:
        raise RuntimeError("run as root inside Linux")
    required = ["ip", "tc", "clang", "bpftool", "go", "gcc", "make", "ping",
                "iptables", "conntrack", "tcpdump", "iperf3", "ethtool", "unshare"]
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        raise RuntimeError("missing tools: " + ", ".join(missing))
    versions = {}
    for name, args in {"go": ["go", "version"], "clang": ["clang", "--version"],
                       "bpftool": ["bpftool", "version"], "iproute2": ["ip", "-V"],
                       "iptables": ["iptables", "--version"]}.items():
        versions[name] = run(args).stdout.strip()
    record("toolchain", versions)
    assert Path("/sys/kernel/btf/vmlinux").stat().st_size > 0
    record("btf", str(Path("/sys/kernel/btf/vmlinux").stat().st_size) + " bytes")
    record("cgroup", run(["stat", "-fc", "%T", "/sys/fs/cgroup"]).stdout.strip())
    fd = os.open("/dev/kvm", os.O_RDWR)
    try:
        api = fcntl.ioctl(fd, 0xae00, 0)
        vm = fcntl.ioctl(fd, 0xae01, 0)
        os.close(vm)
        assert api == 12
        record("kvm", "API=12; KVM_CREATE_VM succeeded")
    finally:
        os.close(fd)

    go_src = REPORT / "hello.go"
    go_src.write_text('package main\nimport "fmt"\nfunc main(){fmt.Println("knd-go-ok")}\n')
    binary = REPORT / "go-smoke"
    run(["go", "build", "-o", binary, go_src], timeout=180)
    assert run([binary]).stdout.strip() == "knd-go-ok"
    record("go_build_run", "compiled and executed Linux amd64 binary")
    btf_header = run(["bpftool", "btf", "dump", "file", "/sys/kernel/btf/vmlinux", "format", "c"], timeout=60)
    (REPORT / "vmlinux.h").write_text(btf_header.stdout)
    RESULT["commands"][-1]["stdout"] = "saved to build/environment/vmlinux.h"
    obj = REPORT / "probe.o"
    run(["clang", "-O2", "-g", "-target", "bpf", "-D__TARGET_ARCH_x86",
         "-I", REPORT, "-c", ROOT / "tests/environment/probe.c", "-o", obj])
    record("bpf_compile", "Clang emitted BPF ELF with BTF")
    features = run(["bpftool", "-j", "feature", "probe", "kernel"], timeout=90)
    (REPORT / "kernel-features.json").write_text(features.stdout)

    owned = []
    pin = Path(tempfile.mkdtemp(prefix="knd-env-bpf-"))
    mounted = False
    label = f"knd-env-{os.getpid()}"
    node, a, b = [label + suffix for suffix in ("-n", "-a", "-b")]
    try:
        run(["mount", "-t", "bpf", "bpf", pin])
        mounted = True
        for name in (node, a, b):
            run(["ip", "netns", "add", name])
            owned.append(name)
            ns(name, "ip", "link", "set", "lo", "up")
        ns(node, "sysctl", "-qw", "net.ipv4.ip_forward=1")
        ns(node, "sysctl", "-qw", "net.ipv4.conf.all.rp_filter=0")
        ns(node, "sysctl", "-qw", "net.ipv4.conf.default.rp_filter=0")
        ns(node, "ip", "link", "add", "knd-gw", "type", "dummy")
        ns(node, "ip", "addr", "add", "169.254.1.1/32", "dev", "knd-gw")
        ns(node, "ip", "link", "set", "knd-gw", "up")
        ns(node, "ip", "route", "add", "blackhole", "10.244.1.0/24")
        for pod, host_if, ip, pod_mac, host_mac in (
            (a, "va", "10.244.1.2", "02:00:00:00:01:02", "02:00:00:00:02:02"),
            (b, "vb", "10.244.1.3", "02:00:00:00:01:03", "02:00:00:00:02:03"),
        ):
            ns(node, "ip", "link", "add", host_if, "type", "veth", "peer", "name", "peer0")
            ns(node, "ip", "link", "set", "peer0", "netns", pod)
            ns(pod, "ip", "link", "set", "peer0", "name", "eth0")
            ns(node, "ip", "link", "set", host_if, "address", host_mac, "up")
            ns(pod, "ip", "link", "set", "eth0", "address", pod_mac, "up")
            ns(pod, "ip", "addr", "add", ip + "/32", "dev", "eth0")
            ns(pod, "ip", "route", "add", "169.254.1.1/32", "dev", "eth0", "scope", "link")
            ns(pod, "ip", "route", "add", "default", "via", "169.254.1.1", "dev", "eth0")
            ns(pod, "ip", "neigh", "replace", "169.254.1.1", "lladdr", host_mac,
               "nud", "permanent", "dev", "eth0")
            ns(node, "ip", "route", "add", ip + "/32", "dev", host_if)
            ns(node, "ip", "neigh", "replace", ip, "lladdr", pod_mac,
               "nud", "permanent", "dev", host_if)
        for pod, dest in ((a, "10.244.1.3"), (b, "10.244.1.2")):
            ping = ns(pod, "ping", "-n", "-c", "3", "-W", "1", dest)
            assert "0% packet loss" in ping.stdout and "ttl=63" in ping.stdout
        record("routed_veth", "two /32 endpoints; bidirectional ping; TTL=63")

        ns(node, "ip", "link", "add", "envvx", "type", "vxlan", "id", "4242",
           "dstport", "4789", "local", "192.0.2.1", "nolearning")
        record("vxlan_device", "isolated VXLAN device creation succeeded; tunnel traffic not tested")

        run(["bpftool", "-d", "prog", "loadall", obj, pin / "programs", "type", "classifier",
             "pinmaps", pin / "maps"])
        record("bpf_load", "verifier accepted TC programs, Map, FIB and CT kfunc references")
        ns(node, "tc", "qdisc", "add", "dev", "va", "clsact")
        ns(node, "tc", "qdisc", "add", "dev", "vb", "clsact")
        ns(node, "tc", "filter", "add", "dev", "va", "ingress", "pref", "10",
           "bpf", "da", "pinned", pin / "programs/env_observe")
        ping = ns(a, "ping", "-n", "-c", "3", "-W", "1", "10.244.1.3")
        assert "ttl=63" in ping.stdout
        lookup = json.loads(run(["bpftool", "-j", "map", "lookup", "pinned", pin / "maps/env_stats",
                                 "key", "hex", "00", "00", "00", "00"]).stdout)
        value = lookup.get("formatted", {}).get("value")
        if not isinstance(value, dict):
            raw = lookup["value"]
            data = bytes(int(x, 16) if isinstance(x, str) else x for x in raw)
            value = dict(zip(("packets", "ct_miss", "ct_hit", "ct_error", "fib_calls", "fib_success"),
                             struct.unpack("<6Q", data)))
        assert value["ct_miss"] >= 3 and value["ct_error"] == 0, value
        assert value["fib_success"] >= 3, value
        record("ct_fib_execution", value)

        ns(node, "tc", "filter", "add", "dev", "vb", "egress", "pref", "10",
           "bpf", "da", "pinned", pin / "programs/env_drop")
        denied = ns(a, "ping", "-n", "-c", "2", "-W", "1", "10.244.1.3", check=False)
        assert denied.returncode != 0 and "100% packet loss" in denied.stdout
        ns(node, "tc", "filter", "del", "dev", "vb", "egress", "pref", "10")
        ns(a, "ping", "-n", "-c", "1", "-W", "1", "10.244.1.3")
        record("tc_egress_drop", "Linux-routed packets blocked, then restored after filter removal")
    finally:
        cleanup_errors = []
        for name in reversed(owned):
            if run(["ip", "netns", "del", name], check=False).returncode:
                cleanup_errors.append(name)
        if mounted and run(["umount", pin], check=False).returncode:
            cleanup_errors.append(str(pin))
        if not cleanup_errors:
            pin.rmdir()
            record("cleanup", "all experiment namespaces and private bpffs removed")
        else:
            raise RuntimeError("cleanup failed: " + str(cleanup_errors))


def main():
    # mount namespace 隔离 bpffs 和 /run/netns 挂载，避免改动 WSL/Docker 全局挂载。
    if os.environ.get("KND_ENV_PRIVATE") != "1":
        raise SystemExit(subprocess.call(["unshare", "--mount", "--propagation", "private",
            "env", "KND_ENV_PRIVATE=1", sys.executable, str(Path(__file__).resolve())]))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        verify()
        RESULT["status"] = "passed"
    except BaseException as exc:
        RESULT["status"] = "failed"
        RESULT["error"] = str(exc)
        print("FAIL " + str(exc), file=sys.stderr, flush=True)
    finally:
        (REPORT / "result.json").write_text(json.dumps(RESULT, indent=2, ensure_ascii=False) + "\n")
    raise SystemExit(0 if RESULT["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
