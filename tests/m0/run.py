#!/usr/bin/env python3
"""M0：真实 redirect、目标 hook、CT/NAT 正反向以及故障注入。"""
import datetime
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build/m0"
OUT.mkdir(parents=True, exist_ok=True)
REPORT = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "checks": {}, "commands": []}


def run(args, check=True, timeout=30):
    p = subprocess.run([str(x) for x in args], text=True, capture_output=True, timeout=timeout)
    REPORT["commands"].append({"argv": [str(x) for x in args], "exit": p.returncode,
                               "stdout": p.stdout, "stderr": p.stderr})
    if check and p.returncode:
        raise RuntimeError(f"{args}: {p.stdout}\n{p.stderr}")
    return p


def passed(name, detail):
    REPORT["checks"][name] = {"status": "passed", "detail": detail}
    print("PASS " + name + ": " + str(detail), flush=True)


def ns(name, *args, **kwargs):
    return run(["ip", "netns", "exec", name, *args], **kwargs)


class Lab:
    def __init__(self):
        self.names = []
        self.processes = []
        self.pin = Path(tempfile.mkdtemp(prefix="knd-m0-bpf-"))
        self.mounted = False
        prefix = f"knd-m0-{os.getpid()}"
        self.node, self.a, self.b = [prefix + x for x in ("-n", "-a", "-b")]

    def __enter__(self):
        try:
            run(["mount", "-t", "bpf", "bpf", self.pin])
            self.mounted = True
            for name in (self.node, self.a, self.b):
                run(["ip", "netns", "add", name])
                self.names.append(name)
                ns(name, "ip", "link", "set", "lo", "up")
            ns(self.node, "sysctl", "-qw", "net.ipv4.ip_forward=1")
            ns(self.node, "sysctl", "-qw", "net.ipv4.conf.all.rp_filter=0")
            ns(self.node, "sysctl", "-qw", "net.ipv4.conf.default.rp_filter=0")
            ns(self.node, "ip", "link", "add", "knd-gw", "type", "dummy")
            ns(self.node, "ip", "addr", "add", "169.254.1.1/32", "dev", "knd-gw")
            ns(self.node, "ip", "link", "set", "knd-gw", "up")
            ns(self.node, "ip", "route", "add", "blackhole", "10.244.1.0/24")
            for pod, interface, suffix in ((self.a, "va", 2), (self.b, "vb", 3)):
                addr = f"10.244.1.{suffix}"
                hm, pm = f"02:00:00:00:02:0{suffix}", f"02:00:00:00:01:0{suffix}"
                ns(self.node, "ip", "link", "add", interface, "type", "veth", "peer", "name", "peer0")
                ns(self.node, "ip", "link", "set", "peer0", "netns", pod)
                ns(pod, "ip", "link", "set", "peer0", "name", "eth0")
                ns(self.node, "ip", "link", "set", interface, "address", hm, "up")
                ns(pod, "ip", "link", "set", "eth0", "address", pm, "up")
                ns(pod, "ip", "addr", "add", addr + "/32", "dev", "eth0")
                ns(pod, "ip", "route", "add", "169.254.1.1/32", "dev", "eth0", "scope", "link")
                ns(pod, "ip", "route", "add", "default", "via", "169.254.1.1", "dev", "eth0")
                ns(pod, "ip", "neigh", "replace", "169.254.1.1", "lladdr", hm, "nud", "permanent", "dev", "eth0")
                ns(self.node, "ip", "route", "add", addr + "/32", "dev", interface)
                ns(self.node, "ip", "neigh", "replace", addr, "lladdr", pm, "nud", "permanent", "dev", interface)
            self.if_a = json.loads(ns(self.node, "ip", "-j", "link", "show", "va").stdout)[0]["ifindex"]
            self.if_b = json.loads(ns(self.node, "ip", "-j", "link", "show", "vb").stdout)[0]["ifindex"]
            run(["bpftool", "-d", "prog", "loadall", OUT / "probe.o", self.pin / "programs",
                 "type", "classifier", "pinmaps", self.pin / "maps"])
            for interface in ("va", "vb"):
                ns(self.node, "tc", "qdisc", "add", "dev", interface, "clsact")
                for direction, prog in (("ingress", "m0_forward"), ("egress", "m0_delivery")):
                    ns(self.node, "tc", "filter", "add", "dev", interface, direction, "pref", "10",
                       "bpf", "da", "pinned", self.pin / "programs" / prog)
            self.config()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def config(self, enabled=1, ct_mode=0, drop=0):
        raw = struct.pack("<5I", enabled, ct_mode, drop, self.if_a, self.if_b)
        run(["bpftool", "map", "update", "pinned", self.pin / "maps/m0_config",
             "key", "hex", "00", "00", "00", "00", "value", "hex", *[f"{x:02x}" for x in raw]])

    def stats(self):
        d = json.loads(run(["bpftool", "-j", "map", "lookup", "pinned", self.pin / "maps/m0_stats",
                            "key", "hex", "00", "00", "00", "00"]).stdout)
        return d["formatted"]["value"]

    def transfer(self, port, dest, udp=False, reverse=False, expect_success=True, server_ns=None):
        server_ns = server_ns or self.b
        log = open(OUT / f"server-{port}.log", "w")
        server = subprocess.Popen(["ip", "netns", "exec", server_ns, "iperf3", "-s", "-1", "-p", str(port)],
                                  stdout=log, stderr=subprocess.STDOUT)
        self.processes.append(server)
        try:
            for _ in range(30):
                if f":{port} " in ns(server_ns, "ss", "-ltnH").stdout:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("iperf listener did not start")
            args = ["iperf3", "-c", dest, "-p", str(port), "-t", "1", "-J", "--connect-timeout", "1500"]
            if udp:
                args += ["-u", "-b", "1M", "-l", "256"]
            if reverse:
                args += ["-R"]
            p = ns(self.a, *args, check=False, timeout=8)
            result = json.loads(p.stdout)
            (OUT / f"client-{port}.json").write_text(json.dumps(result, indent=2))
            if expect_success:
                assert p.returncode == 0 and "error" not in result, result
                assert result["start"]["connected"][0]["remote_host"] == dest, result
                totals = result["end"].get("sum_received", result["end"].get("sum", {}))
                assert totals["bytes"] > 0, result
            else:
                assert p.returncode != 0 or "error" in result, result
            return {"destination": dest, "udp": udp, "reverse": reverse, "success": "error" not in result}
        finally:
            if server.poll() is None:
                server.terminate()
            try:
                server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            log.close()

    def __exit__(self, *_):
        for p in self.processes:
            if p.poll() is None:
                p.kill()
                p.wait()
        errors = []
        for name in reversed(self.names):
            if run(["ip", "netns", "del", name], check=False).returncode:
                errors.append(name)
        if self.mounted and run(["umount", self.pin], check=False).returncode:
            errors.append(str(self.pin))
        if errors:
            raise RuntimeError("cleanup failed: " + str(errors))
        self.pin.rmdir()


def verify():
    header = run(["bpftool", "btf", "dump", "file", "/sys/kernel/btf/vmlinux", "format", "c"]).stdout
    (OUT / "vmlinux.h").write_text(header)
    REPORT["commands"][-1]["stdout"] = "saved vmlinux.h"
    run(["clang", "-O2", "-g", "-target", "bpf", "-D__TARGET_ARCH_x86", "-I", OUT,
         "-c", ROOT / "tests/m0/probe.c", "-o", OUT / "probe.o"])
    with Lab() as lab:
        before = lab.stats()
        for pod, dest in ((lab.a, "10.244.1.3"), (lab.b, "10.244.1.2")):
            output = ns(pod, "ping", "-n", "-c", "3", "-W", "1", dest).stdout
            assert "ttl=63" in output
        after = lab.stats()
        assert after["redirect_requested"] - before["redirect_requested"] == 12, after
        assert after["delivery_seen"] - before["delivery_seen"] >= 12, after
        passed("redirect_ttl", after)
        lab.config(drop=lab.if_b)
        output = ns(lab.a, "ping", "-n", "-c", "2", "-W", "1", "10.244.1.3", check=False)
        assert output.returncode != 0 and "100% packet loss" in output.stdout
        assert lab.stats()["delivery_drop"] >= 2
        lab.config()
        ns(lab.a, "ping", "-n", "-c", "1", "-W", "1", "10.244.1.3")
        passed("redirect_target_egress", "redirected packets blocked at target egress and restored")
        before = lab.stats()
        result = lab.transfer(5201, "10.244.1.3", udp=True)
        assert lab.stats()["redirect_requested"] > before["redirect_requested"]
        passed("direct_udp_tcp_control", result)

        vip = "10.96.0.100"
        for protocol in ("tcp", "udp"):
            ns(lab.node, "iptables", "-t", "nat", "-A", "PREROUTING", "-d", vip,
               "-p", protocol, "-j", "DNAT", "--to-destination", "10.244.1.3")
        before = lab.stats()
        tcp = lab.transfer(5202, vip, reverse=True)
        udp = lab.transfer(5203, vip, udp=True)
        after = lab.stats()
        assert after["ct_original"] > before["ct_original"], after
        assert after["ct_reply"] > before["ct_reply"], after
        passed("dnat_tcp_udp_reply", {"tcp": tcp, "udp": udp, "stats": after})
        hairpin_vip = "10.96.0.101"
        ns(lab.node, "iptables", "-t", "nat", "-A", "PREROUTING", "-d", hairpin_vip,
           "-p", "tcp", "-j", "DNAT", "--to-destination", "10.244.1.2")
        ns(lab.node, "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.244.1.2",
           "-d", "10.244.1.2", "-p", "tcp", "--dport", "5206", "-j", "SNAT", "--to-source", "169.254.1.1")
        before = lab.stats()
        hairpin = lab.transfer(5206, hairpin_vip, server_ns=lab.a)
        after = lab.stats()
        assert after["ct_original"] > before["ct_original"] and after["ct_reply"] > before["ct_reply"]
        passed("hairpin_snat_dnat", hairpin)
        lab.config(ct_mode=2)
        negative = lab.transfer(5204, vip, expect_success=False)
        passed("missing_ct_guard_negative_control", negative)
        lab.config(ct_mode=1)
        before = lab.stats()
        fallback = lab.transfer(5205, "10.244.1.3")
        after = lab.stats()
        assert after["ct_error"] > before["ct_error"]
        assert after["redirect_requested"] == before["redirect_requested"]
        passed("ct_error_kernel_fallback", fallback)
        assert after["mutation_error"] == 0
    passed("cleanup", "all owned namespaces, processes and bpffs removed")


if __name__ == "__main__":
    if os.environ.get("KND_M0_PRIVATE") != "1":
        raise SystemExit(subprocess.call(["unshare", "--mount", "--propagation", "private",
            "env", "KND_M0_PRIVATE=1", sys.executable, str(Path(__file__).resolve())]))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        verify()
        REPORT["status"] = "passed"
    except BaseException as exc:
        REPORT["status"] = "failed"
        REPORT["error"] = str(exc)
        print("FAIL " + str(exc), file=sys.stderr, flush=True)
    finally:
        (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")
    raise SystemExit(0 if REPORT["status"] == "passed" else 1)
