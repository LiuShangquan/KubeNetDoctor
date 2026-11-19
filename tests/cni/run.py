#!/usr/bin/env python3
"""在隔离节点 netns 里通过真实 CNI 协议验证 ADD/CHECK/DEL 与崩溃恢复。"""
import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build/m1"
OUT.mkdir(parents=True, exist_ok=True)
REPORT = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "checks": {}, "commands": []}


def run(args, check=True, timeout=25, **kwargs):
    p = subprocess.run([str(x) for x in args], text=True, capture_output=True, timeout=timeout, **kwargs)
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


def verify():
    prefix = f"knd-m1-{os.getpid()}"
    node = prefix + "-n"
    owned = []
    attachments = {}
    with tempfile.TemporaryDirectory(prefix="knd-m1-state-") as temp:
        state = Path(temp) / "cni"
        leases = Path(temp) / "ipam"
        config = {"cniVersion": "1.0.0", "name": "knd-m1", "type": "knd-cni", "standalone": True,
                  "podCIDR": "10.244.7.0/24", "mtu": 1450, "stateDir": str(state),
                  "ipam": {"type": "host-local", "dataDir": str(leases),
                           "ranges": [[{"subnet": "10.244.7.0/24", "rangeStart": "10.244.7.2"}]]}}

        def pod(label):
            name = prefix + "-" + label
            run(["ip", "netns", "add", name])
            owned.append(name)
            attachments[label] = name
            return name

        def cni(command, label, netns=None, prev=None, injected=None, expect=True):
            conf = dict(config)
            if prev is not None:
                conf["prevResult"] = prev
            sandbox = "/run/netns/" + attachments[label] if netns is None else netns
            env = dict(os.environ, CNI_COMMAND=command, CNI_CONTAINERID=label, CNI_IFNAME="eth0",
                       CNI_NETNS=sandbox, CNI_PATH=str(ROOT / ".cache/cni"))
            env.update(injected or {})
            p = ns(node, ROOT / "build/bin/knd-cni", input=json.dumps(conf), env=env, check=expect)
            if not expect:
                assert p.returncode != 0, p.stdout
                return p
            return json.loads(p.stdout) if p.stdout.strip() else None

        def key(label):
            return hashlib.sha256((config["name"] + "\0" + label + "\0eth0").encode()).hexdigest()

        def assert_gone(label):
            assert not (state / (key(label) + ".json")).exists()
            result = ns(node, "ip", "link", "show", "kn" + key(label)[:11], check=False)
            assert result.returncode != 0
            for entry in (leases / config["name"]).glob("10.244.*"):
                assert entry.read_text().splitlines()[0].strip() != label, entry

        try:
            run(["ip", "netns", "add", node])
            owned.append(node)
            ns(node, "ip", "link", "set", "lo", "up")
            ns(node, "sysctl", "-qw", "net.ipv4.ip_forward=1")
            a, b = pod("a"), pod("b")
            result_a, result_b = cni("ADD", "a"), cni("ADD", "b")
            assert result_a["ips"][0]["address"].endswith("/32")
            ip_a, ip_b = [r["ips"][0]["address"].split("/")[0] for r in (result_a, result_b)]
            assert ip_a != ip_b
            assert "ttl=63" in ns(a, "ping", "-n", "-c", "2", "-W", "1", ip_b).stdout
            assert "ttl=63" in ns(b, "ping", "-n", "-c", "2", "-W", "1", ip_a).stdout
            cni("CHECK", "a", prev=result_a)
            cni("CHECK", "b", prev=result_b)
            assert cni("ADD", "a")["ips"] == result_a["ips"]
            passed("add_check_connectivity", {"a": ip_a, "b": ip_b, "prefix": 32, "ttl": 63})

            ns(a, "ip", "link", "set", "eth0", "mtu", "1400")
            cni("CHECK", "a", prev=result_a, expect=False)
            assert json.loads(ns(a, "ip", "-j", "link", "show", "eth0").stdout)[0]["mtu"] == 1400
            ns(a, "ip", "link", "set", "eth0", "mtu", "1450")
            cni("CHECK", "a", prev=result_a)
            passed("check_detects_drift", "MTU drift rejected without silent repair")

            labels = [f"parallel-{x}" for x in range(8)]
            for label in labels:
                pod(label)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda label: cni("ADD", label), labels))
            allocated = [r["ips"][0]["address"] for r in results]
            assert len(set(allocated + [ip_a + "/32", ip_b + "/32"])) == 10
            for label, result in zip(labels, results):
                cni("CHECK", label, prev=result)
                cni("DEL", label)
                cni("DEL", label)
                assert_gone(label)
            passed("concurrent_add_del", "8 parallel ADD; unique leases; repeated DEL clean")

            for phase in ("allocated", "links", "ready"):
                label = "fail-" + phase
                pod(label)
                cni("ADD", label, injected={"KND_TEST_FAIL_AFTER": phase}, expect=False)
                assert_gone(label)
            passed("rollback", "failures after allocation, link creation and readiness cleaned")

            for phase in ("allocated", "created-host", "links", "ready"):
                label = "crash-" + phase
                pod(label)
                cni("ADD", label, injected={"KND_TEST_CRASH_AFTER": phase}, expect=False)
                assert (state / (key(label) + ".json")).exists()
                cni("DEL", label)
                cni("DEL", label)
                assert_gone(label)
            passed("sigkill_then_del", "durable journal recovers allocation, unmarked host veth, links and ready crashes")

            pod("retry")
            cni("ADD", "retry", injected={"KND_TEST_CRASH_AFTER": "links"}, expect=False)
            result = cni("ADD", "retry")
            cni("CHECK", "retry", prev=result)
            cni("DEL", "retry")
            assert_gone("retry")
            passed("retry_incomplete_add", "incomplete links rolled back before a fresh successful ADD")

            vanished = pod("vanished")
            cni("ADD", "vanished")
            run(["ip", "netns", "del", vanished])
            owned.remove(vanished)
            cni("DEL", "vanished", netns="")
            assert_gone("vanished")
            passed("netns_gone_del", "DEL succeeds after runtime removes namespace")

            pod("collision")
            name = "kn" + key("collision")[:11]
            ns(node, "ip", "link", "add", name, "type", "dummy")
            cni("ADD", "collision", expect=False)
            assert json.loads(ns(node, "ip", "-d", "-j", "link", "show", name).stdout)[0]["linkinfo"]["info_kind"] == "dummy"
            ns(node, "ip", "link", "del", name)
            cni("DEL", "collision")
            assert_gone("collision")
            passed("ownership_collision", "foreign interface preserved; cleanup completed after conflict removal")

            for label in ("a", "b"):
                cni("DEL", label)
                assert_gone(label)
            assert not list(state.glob("*.json"))
            assert not list((leases / config["name"]).glob("10.244.*"))
            passed("final_resources", "0 attachment journals; 0 Pod address leases; shared gateway intentionally retained")
        finally:
            cleanup_errors = []
            if node in owned:
                for label in attachments:
                    try:
                        cni("DEL", label, netns="")
                    except Exception as exc:
                        cleanup_errors.append(str(exc))
            for name in reversed(owned):
                if run(["ip", "netns", "del", name], check=False).returncode:
                    cleanup_errors.append(name)
            if cleanup_errors:
                raise RuntimeError("cleanup errors: " + str(cleanup_errors))
    passed("cleanup", "all experiment namespaces and temporary state removed")


if __name__ == "__main__":
    if os.environ.get("KND_M1_PRIVATE") != "1":
        raise SystemExit(subprocess.call(["unshare", "--mount", "--propagation", "private",
            "env", "KND_M1_PRIVATE=1", sys.executable, str(Path(__file__).resolve())]))
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
