#!/usr/bin/env python3
"""验证复用的 host-local 分配/释放行为；仅使用临时租约目录，不创建网络接口。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / ".cache/cni/host-local"


def main():
    with tempfile.TemporaryDirectory(prefix="knd-env-ipam-") as state:
        config = {"cniVersion": "1.0.0", "name": "knd-env", "type": "host-local",
                  "ipam": {"type": "host-local", "dataDir": state,
                           "ranges": [[{"subnet": "10.244.199.0/29", "rangeStart": "10.244.199.2",
                                        "rangeEnd": "10.244.199.6"}]]}}

        def invoke(command, container):
            env = dict(os.environ, CNI_COMMAND=command, CNI_CONTAINERID=container,
                       CNI_NETNS="/dev/null", CNI_IFNAME="eth0", CNI_PATH=str(PLUGIN.parent))
            p = subprocess.run([str(PLUGIN)], input=json.dumps(config), text=True,
                               capture_output=True, env=env, timeout=15)
            if p.returncode:
                raise RuntimeError(f"{command} failed: {p.stdout} {p.stderr}")
            return json.loads(p.stdout) if p.stdout.strip() else None

        ids = ["knd-env-a", "knd-env-b"]
        addresses = []
        try:
            for container in ids:
                addresses.append(invoke("ADD", container)["ips"][0]["address"])
            assert len(set(addresses)) == 2, addresses
        finally:
            for container in ids:
                invoke("DEL", container)
                invoke("DEL", container)
        leases = [p for p in Path(state).rglob("*") if p.name.startswith("10.244.")]
        assert not leases, leases
        result = {"status": "passed", "plugin": "containernetworking/plugins v1.10.0 host-local",
                  "cni_version": "1.0.0", "addresses": addresses,
                  "duplicate_del": "passed", "leases_after_del": 0}
        out = ROOT / "build/environment/ipam-result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
