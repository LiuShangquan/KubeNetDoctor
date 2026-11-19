#!/usr/bin/env python3
"""两台独立内核 VM 的固定模式测量；共享宿主与虚拟 underlay，不作物理线速结论。"""
import importlib.util
import json
import shlex
from pathlib import Path
import time
import traceback
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('k',ROOT/'tests/kubernetes/run.py');k=importlib.util.module_from_spec(spec);spec.loader.exec_module(k)
mode=k.status(1)['mode'];OUT=ROOT/'build/benchmark'/mode;OUT.mkdir(parents=True,exist_ok=True)
REPORT={'mode':mode,'rows':[],'commands':[]};k.REPORT=REPORT
names=('bench-a','bench-c')
try:
    for n,name in enumerate(names,1):
        k.apply({'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'knd-test','labels':{'knd-benchmark':'owned'}},'spec':{'nodeName':f'knd-node{n}','terminationGracePeriodSeconds':0,'containers':[{'name':'tools','image':'docker.io/library/knd-test:v0.1','imagePullPolicy':'Never','command':['sleep','3600'],'securityContext':{'appArmorProfile':{'type':'Unconfined'}}}]}})
    k.kubectl('-n','knd-test','wait','pod','-l','knd-benchmark=owned','--for=condition=Ready','--timeout=60s',timeout=70)
    time.sleep(2)
    target=json.loads(k.kubectl('-n','knd-test','get','pod','bench-c','-o','json').stdout)['status']['podIP']
    REPORT['benchmarkAppArmor']='Unconfined only for these two test Pods; default Ubuntu stacked profile rejected iperf thread cancellation (audit evidence retained)'
    for iteration in range(3):
        for protocol in ('UDP','TCP'):
            before={n:k.status(n)['counters'] for n in (1,2)}
            port=5301+iteration*10+(1 if protocol=='TCP' else 0)
            server=int(k.inside('bench-c','sh','-c',f'iperf3 -s -1 -p {port} >/tmp/knd-benchmark-{port}.log 2>&1 & echo $!').stdout)
            for _ in range(20):
                if f':{port} ' in k.inside('bench-c','ss','-ltnH').stdout:break
                time.sleep(.1)
            else:raise RuntimeError('one-shot benchmark listener absent')
            args=['timeout','12','iperf3','-c',target,'-p',str(port),'-t','3','-J','--connect-timeout','1000']
            if protocol=='UDP':args+=['-u','-b','100M','-l','128']
            try:
                flow=json.loads(k.inside('bench-a',*args,timeout=18).stdout);assert 'error' not in flow,flow
            finally:
                # 每轮新 server，避免测试镜像中的 iperf pthread_cancel 失败污染下一轮。
                cleanup=f"from pathlib import Path;import os,signal;p=Path('/proc/{server}/cmdline');expected=b'iperf3\\0-s\\0-1\\0-p\\0{port}\\0';os.kill({server},signal.SIGTERM) if p.exists() and p.read_bytes()==expected else None"
                k.inside('bench-c','python3','-c',cleanup)
            after={n:k.status(n)['counters'] for n in (1,2)}
            row={'iteration':iteration,'protocol':protocol,'receivedBps':flow['end']['sum_received']['bits_per_second'],'lostPercent':flow['end']['sum_received'].get('lost_percent',0),'counters':{n:{key:after[n][key]-before[n][key] for key in after[n]} for n in (1,2)},'raw':flow}
            REPORT['rows'].append(row);print(mode,iteration,protocol,round(row['receivedBps']/1e6,2),'Mbit/s',flush=True)
    REPORT['status']='passed'
except BaseException as exc:REPORT['status']='failed';REPORT['error']=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
finally:
    k.kubectl('-n','knd-test','delete','pod',*names,'--ignore-not-found','--wait=true','--timeout=45s',timeout=50)
    (OUT/'result.json').write_text(json.dumps(REPORT,indent=2)+'\n')
raise SystemExit(0 if REPORT['status']=='passed' else 1)
