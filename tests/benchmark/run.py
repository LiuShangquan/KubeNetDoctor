#!/usr/bin/env python3
"""独立 netns 的 B0/B1/B2/B3 对照；每轮重建网络，避免继承 CT，记录真实快慢比例。"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time
import traceback
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'build/benchmark';OUT.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('m2',ROOT/'tests/datapath/run.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
REPORT={'rows':[],'commands':[]};m.REPORT=REPORT;m.m0.REPORT=REPORT;m.OUT=OUT;m.m0.OUT=OUT
ns=m.ns

def workload(lab,protocol,port,duration=3):
    target=lab.snapshot['endpoints'][1]['ip']
    server=subprocess.Popen(['ip','netns','exec',lab.b,'taskset','-c','3','iperf3','-s','-1','-p',str(port)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    try:
        for _ in range(40):
            if f':{port} ' in ns(lab.b,'ss','-ltnH').stdout:break
            time.sleep(.05)
        else:raise RuntimeError('server not ready')
        before=lab.stats()
        args=['taskset','-c','2','iperf3','-c',target,'-p',str(port),'-t',str(duration),'-J','--connect-timeout','1000']
        if protocol=='UDP':args+=['-u','-b','100M','-l','128']
        flow=json.loads(ns(lab.a,*args,timeout=duration+8).stdout);assert 'error' not in flow,flow
        after=lab.stats();delta={key:after[key]-before[key] for key in after}
        receives=flow['end']['sum_received'];sends=flow['end']['sum_sent']
        return {'protocol':protocol,'receivedBps':receives['bits_per_second'],'receivedBytes':receives['bytes'],'sentBps':sends['bits_per_second'],'lostPercent':receives.get('lost_percent',0),'receiverCPU':flow['end']['cpu_utilization_percent']['remote_total'],'senderCPU':flow['end']['cpu_utilization_percent']['host_total'],'counters':delta,'raw':flow}
    finally:
        if server.poll() is None:server.terminate()
        try:server.wait(timeout=2)
        except subprocess.TimeoutExpired:server.kill();server.wait()

def latency(lab):
    target=lab.snapshot['endpoints'][1]['ip'];helper=str(ROOT/'tests/benchmark/udp_latency.py')
    server=subprocess.Popen(['ip','netns','exec',lab.b,'taskset','-c','3','python3',helper,'server',target,'5401'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    try:
        assert server.stderr.readline().strip()=='READY'
        return json.loads(ns(lab.a,'taskset','-c','2','python3',helper,'client',target,'5401').stdout)
    finally:server.terminate();server.wait(timeout=3)

def trial(mode,iteration,profile=False):
    lab=m.Lab()
    if mode=='B3':lab.object=ROOT/'build/bpf/datapath.optimized.o'
    with lab:
        if mode=='B0':
            for ep in lab.snapshot['endpoints']:ns(lab.node,'tc','qdisc','del','dev',ep['hostName'],'clsact')
        elif mode=='B1':lab.snapshot['fastReady']=False;lab.snapshot['generation']+=1;lab.apply()
        row={'mode':mode,'iteration':iteration}
        profiler=None
        try:
            if profile:
                profiler=subprocess.Popen(['ip','netns','exec',lab.node,str(ROOT/'build/bin/knd-profile'),'-pins',str(lab.pin/'knd'),'-duration','6s'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                first=profiler.stderr.readline();assert first.strip()=='READY',first
            row['UDP']=workload(lab,'UDP',5301,4 if profile else 3)
            if profiler:
                output,error=profiler.communicate(timeout=8);assert profiler.returncode==0,error;row['profile']=json.loads(output)
            else:
                row['TCP']=workload(lab,'TCP',5302)
                row['RTT']=latency(lab)
            if mode in ('B2','B3'):assert row['UDP']['counters']['redirect_requested']>1000,row
            if mode in ('B0','B1'):assert row['UDP']['counters']['redirect_requested']==0,row
            assert lab.stats()['drop_mutation']==0
            return row
        finally:
            if profiler and profiler.poll() is None:profiler.kill();profiler.wait()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--modes',default='B0,B1,B2');parser.add_argument('--rounds',type=int,default=5);parser.add_argument('--profile',action='store_true');parser.add_argument('--output',default='result.json');options=parser.parse_args()
    if os.environ.get('KND_BENCH_PRIVATE')!='1':
        return subprocess.call(['unshare','--mount','--propagation','private','env','KND_BENCH_PRIVATE=1',sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]],stdin=subprocess.DEVNULL)
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    REPORT['environment']={'kernel':os.uname().release,'CPU':subprocess.check_output(['lscpu','-J'],text=True),'clientCPU':2,'serverCPU':3,'UDP':'100 Mbit/s, payload 128 bytes','RTT':'Python persistent UDP echo incl application and scheduling overhead','profiling':options.profile,'sharedHostVMsRunning':True}
    sequence=[(mode,iteration) for iteration in range(options.rounds) for mode in options.modes.split(',')];random.Random(17).shuffle(sequence)
    try:
        for mode,iteration in sequence:
            row=trial(mode,iteration,options.profile);REPORT['rows'].append(row)
            print('PASS',mode,iteration,'UDP_Mbps',round(row['UDP']['receivedBps']/1e6,3),'profile',row.get('profile',{}),flush=True)
            (OUT/options.output).write_text(json.dumps(REPORT,indent=2)+'\n')
        REPORT['status']='passed'
    except BaseException as exc:REPORT['status']='failed';REPORT['error']=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
    finally:(OUT/options.output).write_text(json.dumps(REPORT,indent=2)+'\n')
    return 0 if REPORT['status']=='passed' else 1
if __name__=='__main__':raise SystemExit(main())
