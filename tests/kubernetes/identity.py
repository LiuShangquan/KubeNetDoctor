#!/usr/bin/env python3
"""身份漂移必须先阻断；同 IP 的新 UID 不继承旧附件规则。"""
import importlib.util
import ipaddress
import json
from pathlib import Path
import shlex
import time
import traceback
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'build/m5-identity';OUT.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('life',ROOT/'tests/kubernetes/lifecycle.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
k=m.k;REPORT={'checks':{},'commands':[]};k.REPORT=REPORT

def wait_record(name):
    for _ in range(30):
        for r in k.status(1)['records'].values():
            if r['request']['pod']['name']==name:return r
        time.sleep(.2)
    raise RuntimeError('record absent')

def create():
    p=m.fixture('identity',1);p['spec']['containers'][0]['command']=['python3','-m','http.server','8080']
    k.apply(p);k.kubectl('-n','knd-test','wait','pod/identity','--for=condition=Ready','--timeout=60s',timeout=70)
    return wait_record('identity')

def remove():
    k.kubectl('-n','knd-test','delete','pod','identity','--ignore-not-found','--wait=true','--timeout=45s',timeout=50)
    for _ in range(40):
        if not any(r['request']['pod']['name']=='identity' for r in k.status(1)['records'].values()):return
        time.sleep(.5)
    raise RuntimeError('identity record not cleaned')

def verify():
    old=create();ep=old['request']['endpoint'];uid=old['request']['pod']['uid'];m.curl('a',ep['ip'],True)
    try:
        k.ssh(1,shlex.join(['ip','link','set',ep['hostName'],'alias','knd-test-drift']))
        for _ in range(30):
            state=k.status(1)
            if 'ownership changed' in state['lastError']:break
            time.sleep(.2)
        else:raise RuntimeError('ownership drift not detected')
        m.curl('a',ep['ip'],False)
        blocked=wait_record('identity');assert not blocked['ready']
        k.passed('ownership_drift_blocks_before_repair',{'lastError':state['lastError'],'targetIP':ep['ip']})
    finally:k.ssh(1,shlex.join(['ip','link','set',ep['hostName'],'alias',ep['owner']]))
    # 恢复所有权只允许安全删除；显式重建附件才重新授予就绪身份。
    remove()
    m.policy(1,{'rules':[],'blockedUIDs':[uid]})
    # host-local 的游标只是分配提示；测试在旧租约释放后将提示设置为目标前一个 IP。
    previous=str(ipaddress.ip_address(ep['ip'])-1)
    script="python3 -c "+shlex.quote("from pathlib import Path;p=Path('/var/lib/cni/networks/knd/last_reserved_ip.0');assert p.is_file();p.write_text("+repr(previous)+")")
    k.ssh(1,script)
    new=create();nep=new['request']['endpoint']
    assert nep['ip']==ep['ip'] and new['request']['pod']['uid']!=uid
    assert new['id']>old['id'] and new['epoch']>old['epoch']
    m.curl('a',nep['ip'],True)
    k.passed('IP_reuse_new_UID_new_epoch',{'IP':nep['ip'],'oldID':old['id'],'newID':new['id'],'oldUIDBlocked':True,'replacementReachable':True})
    remove();m.policy(1,{'rules':[],'blockedUIDs':[]})

if __name__=='__main__':
    try:verify();REPORT['status']='passed'
    except BaseException as exc:REPORT['status']='failed';REPORT['error']=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
    finally:
        try:m.policy(1,{'rules':[],'blockedUIDs':[]})
        except Exception:pass
        (OUT/'result.json').write_text(json.dumps(REPORT,indent=2)+'\n')
    raise SystemExit(0 if REPORT['status']=='passed' else 1)
