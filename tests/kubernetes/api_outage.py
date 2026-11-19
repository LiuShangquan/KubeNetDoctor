#!/usr/bin/env python3
"""只在专用 VM 节点 2 短暂拒绝 API TCP；验证状态可见、拒绝 fresh prepare、保留已有转发。"""
import importlib.util
import json
from pathlib import Path
import shlex
import time
import traceback
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'build/api-outage';OUT.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('k',ROOT/'tests/kubernetes/run.py');k=importlib.util.module_from_spec(spec);spec.loader.exec_module(k)
REPORT={'checks':{},'commands':[]};k.REPORT=REPORT
rule=['-d','192.168.77.11','-p','tcp','--dport','6443','-m','comment','--comment','knd-test-api-outage','-j','REJECT']
installed=False
try:
    assert 'apiReachable' in k.status(2),'Agent build lacks API health fields'
    record=next(r for r in k.status(2)['records'].values() if r['request']['pod']['name']=='c');ip=record['request']['endpoint']['ip']
    assert k.ssh(2,shlex.join(['iptables','-C','OUTPUT',*rule]),check=False).returncode!=0
    k.ssh(2,shlex.join(['iptables','-I','OUTPUT','1',*rule]));installed=True
    for _ in range(40):
        state=k.status(2)
        if state['apiReachable'] is False:break
        time.sleep(.3)
    else:raise RuntimeError('API outage not reported')
    assert state['apiError']
    assert k.inside('a','curl','--fail','--silent','--max-time','3',f'http://{ip}:8080/').stdout.strip()=='c'
    result=k.ssh(2,"curl -sS --unix-socket /run/knd/agent.sock -w '\\n%{http_code}' -H 'Content-Type: application/json' -d "+shlex.quote(json.dumps(record['request']))+' http://localhost/v1/prepare').stdout.strip()
    body,code=result.rsplit('\n',1);assert code=='409' and json.loads(body)['error'],result
    k.passed('API_outage_retains_dataplane_rejects_prepare',{'apiReachable':False,'existingPodIP':'passed','freshUIDVerification':'HTTP 409; no attachment mutation'})
    REPORT['status']='passed'
except BaseException as exc:REPORT['status']='failed';REPORT['error']=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
finally:
    if installed:k.ssh(2,shlex.join(['iptables','-D','OUTPUT',*rule]))
    if REPORT.get('status')=='passed':
        for _ in range(40):
            if k.status(2)['apiReachable']:break
            time.sleep(.3)
        else:REPORT['status']='failed';REPORT['error']='API health did not recover'
    (OUT/'result.json').write_text(json.dumps(REPORT,indent=2)+'\n')
raise SystemExit(0 if REPORT['status']=='passed' else 1)
