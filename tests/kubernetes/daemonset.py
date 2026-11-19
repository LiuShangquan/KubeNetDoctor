#!/usr/bin/env python3
"""DaemonSet 部署后创建新 sandbox，验证 netns 挂载传播、SA 引导及滚动恢复。"""
import importlib.util
import json
import os
from pathlib import Path
import time
import traceback
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'build/daemonset';OUT.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('native',ROOT/'tests/kubernetes/native.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
k=m.k;REPORT={'checks':{},'commands':[]};k.REPORT=REPORT;m.REPORT=REPORT;m.OUT=OUT;k.OUT=OUT
os.environ['KND_FORWARD_MODE']='vxlan-fast'

def verify():
    k.kubectl('-n','knd-system','rollout','status','daemonset/knd-agent','--timeout=60s',timeout=70)
    for n in (1,2):
        assert k.ssh(n,'systemctl is-active knd-agent',check=False).stdout.strip()=='inactive'
        assert k.ssh(n,'systemctl is-enabled knd-agent',check=False).stdout.strip()=='disabled'
    pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'daemonset-cni-check','namespace':'knd-test'},'spec':{'nodeName':'knd-node2','terminationGracePeriodSeconds':0,'containers':[{'name':'server','image':'docker.io/library/knd-test:v0.1','imagePullPolicy':'Never','command':['python3','-m','http.server','8080']}]}}
    try:
        k.apply(pod);k.kubectl('-n','knd-test','wait','pod/daemonset-cni-check','--for=condition=Ready','--timeout=60s',timeout=70)
        actual=json.loads(k.kubectl('-n','knd-test','get','pod','daemonset-cni-check','-o','json').stdout)
        for _ in range(20):
            result=k.inside('a','curl','--fail','--silent','--max-time','1','http://'+actual['status']['podIP']+':8080/',check=False)
            if result.returncode==0:break
            time.sleep(.2)
        else:raise RuntimeError('new Pod under DaemonSet not reachable')
        record=next(r for r in k.status(2)['records'].values() if r['request']['pod']['name']=='daemonset-cni-check')
        assert record['ready'] and record['request']['pod']['uid']==actual['metadata']['uid']
        k.passed('DaemonSet_new_sandbox_and_netns_propagation',{'podIP':actual['status']['podIP'],'UID':actual['metadata']['uid'],'API':'direct server via projected ServiceAccount credentials'})
    finally:k.kubectl('-n','knd-test','delete','pod','daemonset-cni-check','--ignore-not-found','--wait=true','--timeout=45s',timeout=50)
    for _ in range(30):
        if not any(r['request']['pod']['name']=='daemonset-cni-check' for r in k.status(2)['records'].values()):break
        time.sleep(.3)
    else:raise RuntimeError('temporary endpoint not reaped')
    m.verify()
    agents=json.loads(k.kubectl('-n','knd-system','get','pods','-l','app=knd-agent','-o','json').stdout)['items']
    REPORT['agentPods']=[{'node':p['spec']['nodeName'],'name':p['metadata']['name'],'uid':p['metadata']['uid'],'imageID':p['status']['containerStatuses'][0]['imageID']} for p in agents]

if __name__=='__main__':
    try:verify();REPORT['status']='passed'
    except BaseException as exc:REPORT['status']='failed';REPORT['error']=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
    finally:(OUT/'result.json').write_text(json.dumps(REPORT,indent=2)+'\n')
    raise SystemExit(0 if REPORT['status']=='passed' else 1)
