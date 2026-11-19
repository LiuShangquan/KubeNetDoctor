#!/usr/bin/env python3
"""向两台专用 VM 部署同一构建；不更改宿主或其他集群。"""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import subprocess
import argparse

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("vmlab",ROOT/"scripts/vm-lab.py")
vm=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm)
parser=argparse.ArgumentParser();parser.add_argument("--mode",choices=["kernel","local-fast","native-fast","vxlan-kernel","vxlan-fast"],default="local-fast");options=parser.parse_args()
def ssh(node,script):
 p=subprocess.run(vm.ssh_args(node)+["bash -s"],input=script,text=True,capture_output=True,timeout=60)
 if p.returncode:raise RuntimeError(f"node{node}: {p.stdout}\n{p.stderr}")
 return p.stdout
def copy(node,source,target):
 subprocess.run(["scp","-i",str(vm.KEY),"-P",str(22270+node),"-o","BatchMode=yes","-o","UserKnownHostsFile="+str(vm.WORK/"known_hosts"),str(source),"root@127.0.0.1:"+target],check=True,timeout=90,stdout=subprocess.DEVNULL)
def deploy(node):
 ssh(node,"set -e\nmkdir -p /opt/knd /opt/cni/bin /sys/fs/bpf\nmountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf\n")
 for source,target in [(ROOT/"build/bin/knd-cni","/opt/cni/bin/knd-cni"),(ROOT/".cache/cni/host-local","/opt/cni/bin/host-local"),(ROOT/".cache/cni/loopback","/opt/cni/bin/loopback"),
                       (ROOT/"build/bin/kndctl","/opt/knd/kndctl"),(ROOT/"build/bin/knd-agent","/opt/knd/knd-agent"),(ROOT/"build/bin/knd-datapath","/opt/knd/knd-datapath"),(ROOT/"build/bpf/datapath.o","/opt/knd/datapath.o"),(ROOT/"build/bpf/datapath.baseline.o","/opt/knd/datapath.baseline.o")]:
  copy(node,source,target+".new")
  ssh(node,f"set -e\nchmod 0755 {target}.new\nmv -f {target}.new {target}\n")
 # 读取真实 containerd 配置并核验，避免把插件放到未被使用的目录。
 info=json.loads(ssh(node,"/usr/local/bin/k3s crictl info\n"))["config"]["cni"]
 if info["confDir"]!="/etc/cni/net.d" or "/opt/cni/bin" not in info["binDirs"]:raise RuntimeError("unexpected runtime CNI paths")
 unit=f'''[Unit]
Description=KubeNetDoctor development node agent
After=network-online.target knd-k3s.service
Wants=network-online.target
[Service]
ExecStart=/opt/knd/knd-agent -node knd-node{node} -underlay underlay0 -mode {options.mode} -kubeconfig /opt/knd/kubeconfig -object /opt/knd/datapath.o
Restart=on-failure
RestartSec=3
LimitMEMLOCK=infinity
[Install]
WantedBy=multi-user.target
'''
 ssh(node,"set -e\ncat > /etc/systemd/system/knd-agent.service <<'KND_UNIT'\n"+unit+"KND_UNIT\nsystemctl daemon-reload\nsystemctl enable knd-agent\nsystemctl restart knd-agent\n")
 return {"node":node,"runtimeCNI":info}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 for result in pool.map(deploy,(1,2)):print(json.dumps(result),flush=True)
