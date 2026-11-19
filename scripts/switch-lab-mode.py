#!/usr/bin/env python3
"""只在专用集群受控删除演示附件后切换模式，不提供在线无损切换。"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import time
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('k',ROOT/'tests/kubernetes/run.py');k=importlib.util.module_from_spec(spec);spec.loader.exec_module(k)
parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['kernel','local-fast','native-fast','vxlan-kernel','vxlan-fast']);args=parser.parse_args()
pods=json.loads(k.kubectl('get','pods','-A','-o','json').stdout)['items']
for p in pods:
    if p['spec'].get('hostNetwork') or p['status'].get('phase') in ('Succeeded','Failed'):continue
    m=p['metadata']
    if not ((m['namespace']=='knd-test' and m['name'] in ('a','b','c')) or (m['namespace']=='kube-system' and m['name'].startswith('coredns-'))):raise RuntimeError('unexpected active Pod; refused mode change')
k.kubectl('-n','kube-system','scale','deployment','coredns','--replicas=0')
k.kubectl('-n','knd-test','delete','pod','a','b','c','--ignore-not-found','--wait=true','--timeout=45s',timeout=50)
for n in (1,2):
    for _ in range(40):
        if not k.status(n)['records']:break
        time.sleep(.5)
    else:raise RuntimeError('live attachment remains')
ds=k.kubectl('-n','knd-system','get','daemonset','knd-agent',check=False)
if ds.returncode==0:
    k.kubectl('-n','knd-system','delete','daemonset','knd-agent','--wait=true','--timeout=60s',timeout=70)
    k.kubectl('-n','knd-system','wait','pod','-l','app=knd-agent','--for=delete','--timeout=60s',timeout=70)
subprocess.run(['python3',str(ROOT/'scripts/deploy-lab-agent.py'),'--mode',args.mode],check=True)
for n in (1,2):
    for _ in range(30):
        try:
            s=k.status(n)
            if s['localReady'] and s['mode']==args.mode and not s['lastError']:break
        except Exception:pass
        time.sleep(.5)
    else:
        print(k.ssh(n,'journalctl -u knd-agent -n 20 --no-pager').stdout)
        raise RuntimeError('new mode not ready')
k.kubectl('-n','kube-system','scale','deployment','coredns','--replicas=1')
print('mode ready; recreate fixtures with tests/kubernetes/run.py',args.mode,flush=True)
