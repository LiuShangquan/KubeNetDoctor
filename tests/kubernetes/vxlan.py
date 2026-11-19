#!/usr/bin/env python3
"""VXLAN 内外层抓包及 MTU 边界；可在 kernel/fast 两种隧道模式执行。"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import traceback
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('native',ROOT/'tests/kubernetes/native.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
k=m.k;mode=k.status(1)['mode'];OUT=ROOT/'build/m6'/mode;OUT.mkdir(parents=True,exist_ok=True)
REPORT={'mode':mode,'checks':{},'commands':[]};k.REPORT=REPORT

def verify():
    assert mode in ('vxlan-kernel','vxlan-fast')
    if mode=='vxlan-fast':
        # 两次跨节点 eBPF 转发与完整 Service 用例由 native.py 在同模式重复执行。
        pass
    state=k.status(1);a=next(r for r in state['records'].values() if r['request']['pod']['name']=='a')['request']['endpoint']
    target=next(r for r in k.status(2)['records'].values() if r['request']['pod']['name']=='c')['request']['endpoint']
    command="timeout 5 tcpdump -i underlay0 -nn -vvv -l -c 8 'udp port 4789'"
    process=subprocess.Popen(k.vm.ssh_args(1)+[command],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        time.sleep(.5)
        result=k.inside('a','curl','--fail','--silent','--max-time','2',f'http://{target["ip"]}:8080/');assert result.stdout.strip()=='c'
        output,error=process.communicate(timeout=8)
        assert 'VXLAN' in output and 'vni 42' in output and a['ip'] in output and target['ip'] in output,output+error
        assert '192.168.77.11' in output and '192.168.77.12' in output
        (OUT/'outer-inner.txt').write_text(output+error)
        k.passed('VXLAN_outer_and_inner_capture',{'outer':['192.168.77.11','192.168.77.12'],'UDP':4789,'VNI':42,'inner':[a['ip'],target['ip']]})
    finally:
        if process.poll() is None:process.kill();process.wait()
    prefix=['nsenter','--net='+a['netns'],'ping','-M','do','-c','2','-W','1']
    good=k.ssh(1,shlex.join(prefix+['-s','1422',target['ip']]));assert '0% packet loss' in good.stdout
    bad=k.ssh(1,shlex.join(prefix+['-s','1423',target['ip']]),check=False);assert bad.returncode!=0
    k.passed('VXLAN_MTU_boundary',{'underlay':1500,'Pod':1450,'IP1450':'success','IP1451':'local rejection'})
    for n in (1,2):
        for name,command in [('link','ip -d link show kndvx0'),('route','ip route show proto 190'),('neighbors','ip neigh show dev kndvx0'),('FDB','bridge fdb show dev kndvx0')]:
            (OUT/f'node{n}-{name}.txt').write_text(k.ssh(n,command).stdout)

if __name__=='__main__':
    try:verify();REPORT['status']='passed'
    except BaseException as exc:REPORT['status']='failed';REPORT['error']=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
    finally:(OUT/'result.json').write_text(json.dumps(REPORT,indent=2)+'\n')
    raise SystemExit(0 if REPORT['status']=='passed' else 1)
