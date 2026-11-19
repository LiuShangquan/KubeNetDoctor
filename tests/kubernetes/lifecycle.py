#!/usr/bin/env python3
"""M5：UID 规则、快慢路径门禁、离线 DEL 与 100 次真实 runtime 生命周期。"""
import base64
import datetime
import importlib.util
import json
import os
from pathlib import Path
import shlex
import time
import traceback

ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/os.environ.get('KND_TEST_OUT','build/m5');OUT.mkdir(parents=True,exist_ok=True)
REPORT={'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':{},'commands':[]}
spec=importlib.util.spec_from_file_location('k',ROOT/'tests/kubernetes/run.py');k=importlib.util.module_from_spec(spec);spec.loader.exec_module(k)
k.OUT=OUT;k.REPORT=REPORT
ssh,kubectl,inside,status,passed=k.ssh,k.kubectl,k.inside,k.status,k.passed

def policy(node,document,expected=200):
    command="curl --silent --show-error --unix-socket /run/knd/agent.sock -w '\\n%{http_code}' -H 'Content-Type: application/json' -d "+shlex.quote(json.dumps(document))+" http://localhost/v1/policy"
    response=ssh(node,command).stdout.strip();body,code=response.rsplit('\n',1)
    assert int(code)==expected,response
    return json.loads(body)

def curl(pod,ip,allowed):
    response=inside(pod,'curl','--fail','--silent','--max-time','1',f'http://{ip}:8080/',check=False)
    assert (response.returncode==0)==allowed,(pod,ip,allowed,response.stdout,response.stderr)

def inventory(node):
    command="""python3 -c 'import json,glob,os,subprocess;print(json.dumps({"leases":sorted(os.path.basename(p) for p in glob.glob("/var/lib/cni/networks/knd/*") if os.path.basename(p)[0].isdigit()),"journals":len(glob.glob("/var/lib/knd/cni/*.json")),"veth":sorted(x["ifname"] for x in json.loads(subprocess.check_output(["ip","-j","link"])) if x.get("ifalias","").startswith("knd:v1:"))}))'"""
    return json.loads(ssh(node,command).stdout)

def fixture(name,node):
    return {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'knd-test','labels':{'knd-lifecycle':'owned'}},'spec':{'nodeName':f'knd-node{node}','terminationGracePeriodSeconds':0,'containers':[{'name':'sleep','image':'docker.io/library/knd-test:v0.1','imagePullPolicy':'Never','command':['sleep','3600']}]}}

def wait_clean(baseline):
    for _ in range(30):
        actual={n:inventory(n) for n in (1,2)}
        if actual==baseline:return actual
        time.sleep(.5)
    raise AssertionError({'baseline':baseline,'actual':actual})

def verify():
    for n in (1,2):policy(n,{'rules':[],'blockedUIDs':[]})
    pods=json.loads(kubectl('-n','knd-test','get','pods','-o','json').stdout)['items']
    uids={p['metadata']['name']:p['metadata']['uid'] for p in pods};ips={p['metadata']['name']:p['status']['podIP'] for p in pods}
    local=json.loads(kubectl('-n','knd-test','get','svc','local','-o','json').stdout)['spec']['clusterIP']
    rules=[{'sourceUID':uids['a'],'destinationUID':uids['b'],'protocol':proto} for proto in (6,17)]
    before=status(1)['counters'];policy(1,{'rules':rules,'blockedUIDs':[]})
    curl('a',ips['b'],False);curl('a',local,False);curl('a',ips['c'],True)
    output=inside('a','sh','-c',f"printf denied | socat -T 1 - UDP4-DATAGRAM:{local}:8081").stdout;assert output==''
    after=status(1)['counters'];assert after['drop_acl']>before['drop_acl']
    passed('ACL_local_direct_and_DNAT_target',{'TCP':'direct PodIP and ClusterIP blocked','UDP':'ClusterIP blocked','unrelatedRemote':'allowed','dropDelta':after['drop_acl']-before['drop_acl']})
    # 非首片依然按 L3 身份和 protocol 检查，不能通过缺少传输头绕过规则。
    records={r['request']['pod']['name']:r for r in status(1)['records'].values() if r['request']['pod']['namespace']=='knd-test'}
    endpoint=records['a']['request']['endpoint'];packet={'source':ips['a'],'destination':ips['b'],'hostMAC':endpoint['hostMAC'],'podMAC':endpoint['podMAC'],'fragment':1}
    encoded=base64.b64encode((ROOT/'tests/datapath/packet.py').read_bytes()).decode()
    inside('a','sh','-c',f'printf %s {encoded} | base64 -d > /tmp/knd-packet.py')
    before=status(1)['counters']['drop_acl'];inside('a','python3','/tmp/knd-packet.py','send',json.dumps(packet));assert status(1)['counters']['drop_acl']>before
    policy(1,{'rules':[{'sourceUID':uids['a'],'destinationUID':uids['b'],'protocol':1}]},400)
    assert status(1)['policy']['rules']==rules;curl('a',local,False)
    passed('ACL_fragment_and_invalid_update','noninitial fragment denied; unsupported update rejected and live policy retained')
    policy(1,{'rules':[],'blockedUIDs':[uids['b']]});curl('a',ips['b'],False)
    direct=ssh(1,f'curl --fail --silent --max-time 1 http://{ips["b"]}:8080/',check=False);assert direct.returncode!=0
    policy(1,{'rules':[],'blockedUIDs':[]});curl('a',ips['b'],True)
    passed('endpoint_block_fast_and_kernel_source','UID block acknowledged after map readback; Pod and host traffic denied; unblocking restores traffic')
    # 只在目标节点发布跨节点 ACL，验证接收端独立执行，不依赖源节点策略。
    policy(2,{'rules':[{'sourceUID':uids['a'],'destinationUID':uids['c'],'protocol':p} for p in (6,17)],'blockedUIDs':[]})
    curl('a',ips['c'],False);curl('a',ips['b'],True)
    before=status(2)['counters']['drop_acl'];packet['destination']=ips['c'];packet['fragment']=0;packet['sport']=41271
    inside('a','python3','/tmp/knd-packet.py','send',json.dumps(packet));assert status(2)['counters']['drop_acl']>before
    policy(2,{'rules':[],'blockedUIDs':[]});curl('a',ips['c'],True)
    passed('ACL_remote_receiver','target node denies TCP and fresh UDP tuple independently of source policy')
    if os.environ.get('KND_ACL_ONLY')=='1':return
    baseline={n:inventory(n) for n in (1,2)}
    k.apply(fixture('offline-del',1));kubectl('-n','knd-test','wait','pod/offline-del','--for=condition=Ready','--timeout=60s',timeout=70)
    ssh(1,'systemctl stop knd-agent')
    try:
        kubectl('-n','knd-test','delete','pod','offline-del','--wait=true','--timeout=45s',timeout=50)
        # API 对象消失早于 kubelet/CRI 完成异步 sandbox 清理，等待实际 CNI DEL。
        for _ in range(40):
            inv=inventory(1)
            if inv['leases']==baseline[1]['leases'] and inv['veth']==baseline[1]['veth']:break
            time.sleep(.5)
        assert inv['leases']==baseline[1]['leases'] and inv['veth']==baseline[1]['veth'] and inv['journals']==baseline[1]['journals']+1,(baseline,inv)
    finally:ssh(1,'systemctl start knd-agent')
    wait_clean(baseline)
    passed('offline_DEL_tombstone_reaped','runtime DEL frees owned veth and IPAM while Agent is stopped; restart removes detached journal')
    identities=[]
    for batch in range(10):
        items=[fixture(f'churn-{batch}-{i}',1+i%2) for i in range(10)]
        k.apply({'apiVersion':'v1','kind':'List','items':items})
        kubectl('-n','knd-test','wait','pod','-l','knd-lifecycle=owned','--for=condition=Ready','--timeout=90s',timeout=100)
        active=json.loads(kubectl('-n','knd-test','get','pods','-l','knd-lifecycle=owned','-o','json').stdout)['items']
        assert len(active)==10 and len({p['status']['podIP'] for p in active})==10
        identities.extend((p['metadata']['uid'],p['status']['podIP']) for p in active)
        kubectl('-n','knd-test','delete','pod','-l','knd-lifecycle=owned','--wait=true','--timeout=60s',timeout=70)
        wait_clean(baseline);print('CYCLE',10*(batch+1),flush=True)
    assert len({uid for uid,ip in identities})==100
    passed('100_runtime_lifecycles_no_leak',{'pods':100,'batch':10,'finalInventory':baseline,'uniqueUIDs':100})
    for n in (1,2):
        s=status(n);assert not s['lastError'] and s['appliedGeneration']==s['desiredGeneration'];assert s['counters']['drop_mutation']==0

if __name__=='__main__':
    try:verify();REPORT['status']='passed'
    except BaseException as exc:REPORT['status']='failed';REPORT['error']=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
    finally:
        for n in (1,2):
            try:policy(n,{'rules':[],'blockedUIDs':[]})
            except Exception:pass
        (OUT/'result.json').write_text(json.dumps(REPORT,indent=2)+'\n')
    raise SystemExit(0 if REPORT['status']=='passed' else 1)
