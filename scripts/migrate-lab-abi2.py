#!/usr/bin/env python3
"""专用实验集群的受控停机升级：回收 Pod 后卸载 ABI1，再启动 ABI2。不是在线无损升级。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import argparse
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("vm",ROOT/"scripts/vm-lab.py");vm=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm)
parser=argparse.ArgumentParser();parser.add_argument("--from-abi",type=int,choices=[1,2],default=1);options=parser.parse_args()
def ssh(node,script):
 p=subprocess.run(vm.ssh_args(node)+["bash -s"],input=script,text=True,capture_output=True,timeout=60)
 if p.returncode:raise RuntimeError(p.stdout+p.stderr)
 return p.stdout
pods=json.loads(ssh(1,"/usr/local/bin/k3s kubectl get pods -A -o json\n"))["items"]
for p in pods:
 if p["spec"].get("hostNetwork") or p["status"].get("phase") in ("Succeeded","Failed"):continue
 meta=p["metadata"]
 if not ((meta["namespace"]=="knd-test" and meta["name"] in ("a","b","c")) or (meta["namespace"]=="kube-system" and meta["name"].startswith("coredns-"))):raise RuntimeError("unexpected active Pod; refused to reset network")
ssh(1,"set -e\n/usr/local/bin/k3s kubectl scale deployment -n kube-system coredns --replicas=0\n/usr/local/bin/k3s kubectl delete pod -n knd-test a b c --ignore-not-found --wait=true --timeout=30s\n")
for node in (1,2):
 for _ in range(30):
  state=json.loads(ssh(node,"curl -fsS --unix-socket /run/knd/agent.sock http://localhost/v1/status\n"))
  if not state["records"]:break
  time.sleep(1)
 else:raise RuntimeError("live endpoint remains; refused unpin")
 ssh(node,"systemctl stop knd-agent\n")
 if options.from_abi==2:
  ssh(node,"""python3 - <<'PY'
import json,subprocess
filters=json.loads(subprocess.check_output(['tc','-j','filter','show','dev','underlay0','ingress']))
for f in filters:
 if f.get('pref')==49152 and f.get('options'):
  if f.get('kind')!='bpf' or f['options'].get('bpf_name')!='knd:knd_from_network' or f['options'].get('handle')!='0x1':raise RuntimeError('foreign TC filter')
  subprocess.run(['tc','filter','del','dev','underlay0','ingress','pref','49152'],check=True)
PY
""")
 ssh(node,"""python3 - <<'PY'
from pathlib import Path
p=Path('/sys/fs/bpf/knd/vVERSION')
allowed={'active_snapshot','bindings','runtime_config','reason_counters','knd_from_pod','knd_to_pod','knd_from_network'}
if p.exists():
 if {f.name for f in p.iterdir()}-allowed:raise RuntimeError('unexpected pins')
 for name in allowed:
  f=p/name
  if f.exists():f.unlink()
 p.rmdir()
print('old ABI1 unpinned after all Pod interfaces were removed')
PY
""".replace('vVERSION','v'+str(options.from_abi)))
print("ABI1 stopped and cleared; deploy ABI2 then restore CoreDNS replicas=1 and test fixtures",flush=True)
