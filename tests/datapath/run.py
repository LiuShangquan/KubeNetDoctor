#!/usr/bin/env python3
"""真实 CNI + 正式 BPF ELF + Go 加载器的隔离同节点验收。"""
import copy
import datetime
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/os.environ.get('KND_TEST_OUT','build/m2');OUT.mkdir(parents=True,exist_ok=True)
REPORT={"time":datetime.datetime.now(datetime.timezone.utc).isoformat(),"checks":{},"commands":[]}
spec=importlib.util.spec_from_file_location("m0lab",ROOT/"tests/m0/run.py")
m0=importlib.util.module_from_spec(spec);spec.loader.exec_module(m0)
m0.OUT=OUT;m0.REPORT=REPORT
run,ns,passed=m0.run,m0.ns,m0.passed

class Lab(m0.Lab):
    def __init__(self):
        super().__init__()
        if os.environ.get('KND_DATAPATH_OBJECT'):self.object=Path(os.environ['KND_DATAPATH_OBJECT'])
        self.temp=tempfile.TemporaryDirectory(prefix="knd-m2-state-")
        self.state=Path(self.temp.name)/"cni"
        self.conf={"cniVersion":"1.0.0","name":"knd-m2","type":"knd-cni","standalone":True,
          "podCIDR":"10.244.1.0/24","stateDir":str(self.state),"mtu":1450,
          "ipam":{"type":"host-local","dataDir":str(Path(self.temp.name)/"ipam"),"ranges":[[{"subnet":"10.244.1.0/24","rangeStart":"10.244.1.2"}]]}}
        self.attachments=[]

    def cni(self,command,pod):
        env=dict(os.environ,CNI_COMMAND=command,CNI_CONTAINERID=pod,CNI_IFNAME="eth0",CNI_NETNS="/run/netns/"+pod,CNI_PATH=str(ROOT/".cache/cni"))
        p=subprocess.run(["ip","netns","exec",self.node,str(ROOT/"build/bin/knd-cni")],env=env,input=json.dumps(self.conf),capture_output=True,text=True,timeout=15)
        REPORT["commands"].append({"cni":command,"pod":pod,"exit":p.returncode,"stdout":p.stdout,"stderr":p.stderr})
        assert p.returncode==0,p.stdout+p.stderr

    def __enter__(self):
        try:
            run(["mount","-t","bpf","bpf",self.pin]);self.mounted=True
            for name in (self.node,self.a,self.b):
                run(["ip","netns","add",name]);self.names.append(name)
                ns(name,"ip","link","set","lo","up")
            ns(self.node,"sysctl","-qw","net.ipv4.ip_forward=1")
            ns(self.node,"sysctl","-qw","net.ipv4.conf.all.rp_filter=0")
            ns(self.node,"sysctl","-qw","net.ipv4.conf.default.rp_filter=0")
            for pod in (self.a,self.b):
                self.attachments.append(pod);self.cni("ADD",pod)
            records=[json.loads(p.read_text()) for p in self.state.glob("*.json")]
            records.sort(key=lambda x:x["ip"])
            fields=["owner","ifName","netns","netnsInode","hostName","hostIndex","hostMAC","podMAC","ip"]
            self.snapshot={"generation":1,"serviceCIDR":"10.96.0.0/12","mtu":1450,"fastReady":True,"endpoints":[dict({k:r[k] for k in fields},id=i+1,epoch=1,ready=True) for i,r in enumerate(records)]}
            self.apply()
            return self
        except BaseException:
            self.__exit__(None,None,None);raise

    def apply(self,expect=True):
        path=Path(self.temp.name)/"snapshot.json";path.write_text(json.dumps(self.snapshot))
        p=ns(self.node,ROOT/"build/bin/knd-datapath","-pins",self.pin/"knd","-object",getattr(self,"object",ROOT/"build/bpf/datapath.o"),"-apply",path,check=expect)
        if not expect:assert p.returncode!=0

    def stats(self):
        return json.loads(ns(self.node,ROOT/"build/bin/knd-datapath","-pins",self.pin/"knd","-stats").stdout)

    def packet(self,source_index=0,capture=True,**changes):
        src,dst=self.snapshot["endpoints"][source_index],self.snapshot["endpoints"][1-source_index]
        c={"source":src["ip"],"destination":dst["ip"],"hostMAC":src["hostMAC"],"podMAC":src["podMAC"]};c.update(changes)
        src_ns=self.a if source_index==0 else self.b;dst_ns=self.b if source_index==0 else self.a
        proc=None
        try:
            if capture:
                proc=subprocess.Popen(["ip","netns","exec",dst_ns,"python3",str(ROOT/"tests/datapath/packet.py"),"recv",json.dumps(c)],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                self.processes.append(proc)
                assert proc.stderr.readline().strip()=="READY"
            ns(src_ns,"python3",ROOT/"tests/datapath/packet.py","send",json.dumps(c))
            if proc:
                out,err=proc.communicate(timeout=5);assert proc.returncode==0,err
                result=json.loads(out);assert result["ttl"]==63 and result["checksumValid"],result
                assert result["dstMAC"]==dst["podMAC"] and result["srcMAC"]==dst["hostMAC"],result
                return result
            time.sleep(0.05)
        finally:
            if proc and proc.poll() is None:proc.kill();proc.wait()

    def __exit__(self,*args):
        try:
            for pod in self.attachments:self.cni("DEL",pod)
        finally:
            try:super().__exit__(*args)
            finally:self.temp.cleanup()

def verify():
    with Lab() as lab:
        before=lab.stats()
        captures=[lab.packet(source_index=0),lab.packet(source_index=1,sport=40002)]
        after=lab.stats()
        assert after["redirect_requested"]-before["redirect_requested"]==2,after
        assert after["delivery_fast_seen"]-before["delivery_fast_seen"]==2,after
        assert after["delivery_seen"]>before["delivery_seen"],after
        passed("udp_redirect_ttl_mac",captures)
        # 在目标 egress 的最终检查之前注入旧 epoch，证明真正 redirect 的包也被拒绝。
        run(["clang","-O2","-g","-target","bpf","-I",ROOT/"build/bpf","-I",ROOT/"bpf","-c",ROOT/"tests/datapath/stale_epoch.c","-o",OUT/"stale_epoch.o"])
        run(["bpftool","prog","loadall",OUT/"stale_epoch.o",lab.pin/"stale","type","classifier"])
        target=lab.snapshot["endpoints"][1]
        ns(lab.node,"tc","filter","add","dev",target["hostName"],"egress","pref","10","bpf","da","pinned",lab.pin/"stale/test_stale_epoch")
        before=lab.stats();lab.packet(capture=False,sport=40015);after=lab.stats()
        assert after["redirect_requested"]==before["redirect_requested"]+1 and after["drop_endpoint"]>before["drop_endpoint"],after
        ns(lab.node,"tc","filter","del","dev",target["hostName"],"egress","pref","10")
        lab.packet(sport=40016)
        passed("redirect_target_epoch_gate","fast redirect rejected at target when expected epoch differs; removal restores delivery")
        before=lab.stats()
        ns(lab.a,"ping","-n","-c","2","-W","1","10.244.1.3")
        after=lab.stats();assert after["redirect_requested"]==before["redirect_requested"] and after["slow_protocol"]>before["slow_protocol"]
        passed("icmp_linux_fallback","ICMP follows Linux; no redirect")
        for case,changes,reason in [
          ("source_ip",{"source":"10.244.1.99"},"drop_source"),
          ("source_mac",{"podMAC":"02:00:00:99:99:99"},"drop_source"),
          ("checksum",{"bad_checksum":True},"drop_parse"),
          ("udp_length",{"bad_udp":True},"drop_parse"),
          ("tcp_truncated",{"truncated_tcp":True},"drop_parse"),
          ("ttl",{"ttl":1},"slow_ttl"),
        ]:
            before=lab.stats();lab.packet(capture=False,**changes);after=lab.stats()
            assert after[reason]>before[reason] and after["redirect_requested"]==before["redirect_requested"],(case,before,after)
        passed("invalid_source_and_headers","spoofed source, bad checksum/UDP/TCP rejected; TTL=1 falls back unmodified")
        before=lab.stats();capture=lab.packet(options="01010000",sport=40003);after=lab.stats()
        assert after["slow_options_fragment"]>before["slow_options_fragment"] and after["redirect_requested"]==before["redirect_requested"]
        passed("options_no_double_ttl",capture)
        # 邻居缺失必须回退；Linux ARP 恢复后仍是单次 TTL。
        target=lab.snapshot["endpoints"][1]
        ns(lab.node,"ip","neigh","del",target["ip"],"dev",target["hostName"])
        before=lab.stats();capture=lab.packet(sport=40004);after=lab.stats()
        assert after["slow_neigh"]>before["slow_neigh"]
        ns(lab.node,"ip","neigh","replace",target["ip"],"lladdr",target["podMAC"],"nud","permanent","dev",target["hostName"])
        passed("neighbor_fallback",capture)
        before=lab.stats();lab.transfer(5201,"10.244.1.3",udp=True);after=lab.stats()
        assert after["redirect_requested"]>before["redirect_requested"]
        passed("real_udp_tcp_control","iperf3 UDP plus TCP control succeeds")
        vip="10.96.0.100"
        for protocol in ("tcp","udp"):
            ns(lab.node,"iptables","-t","nat","-A","PREROUTING","-d",vip,"-p",protocol,"-j","DNAT","--to-destination","10.244.1.3")
        before=lab.stats();tcp=lab.transfer(5202,vip,reverse=True);udp=lab.transfer(5203,vip,udp=True);after=lab.stats()
        assert after["slow_service"]>before["slow_service"] and after["slow_ct_hit"]>before["slow_ct_hit"],after
        passed("dnat_tcp_udp",{"tcp":tcp,"udp":udp})
        hvip="10.96.0.101"
        ns(lab.node,"iptables","-t","nat","-A","PREROUTING","-d",hvip,"-p","tcp","-j","DNAT","--to-destination","10.244.1.2")
        ns(lab.node,"iptables","-t","nat","-A","POSTROUTING","-s","10.244.1.2","-d","10.244.1.2","-p","tcp","--dport","5206","-j","SNAT","--to-source","169.254.1.1")
        passed("hairpin_snat_dnat",lab.transfer(5206,hvip,server_ns=lab.a))
        # 配置错误必须保留先前完整快照。
        saved=copy.deepcopy(lab.snapshot);lab.snapshot["endpoints"].append(lab.snapshot["endpoints"][0]);lab.apply(expect=False)
        lab.snapshot=saved;lab.packet(sport=40010)
        passed("invalid_snapshot_preserves_live","duplicate endpoint rejected before map mutation")
        # 阻断目的端必须覆盖 Linux 慢路径。恢复后重新核对实际交付。
        lab.snapshot["endpoints"][1]["ready"]=False;lab.snapshot["generation"]+=1;lab.apply()
        before=lab.stats();lab.packet(capture=False,sport=40011);after=lab.stats()
        assert after["drop_endpoint"]>before["drop_endpoint"] and after["redirect_requested"]==before["redirect_requested"]
        lab.snapshot["endpoints"][1]["ready"]=True;lab.snapshot["generation"]+=1;lab.apply();lab.packet(sport=40012)
        passed("target_endpoint_gate","blocked target cannot be delivered through Linux fallback; restored after publish")
        REPORT["final_counters"]=lab.stats();assert REPORT["final_counters"]["drop_mutation"]==0
    passed("cleanup","all CNI leases, test processes, netns and bpffs cleaned")

if __name__=="__main__":
    if os.environ.get("KND_M2_PRIVATE")!="1":
        raise SystemExit(subprocess.call(["unshare","--mount","--propagation","private","env","KND_M2_PRIVATE=1",sys.executable,str(Path(__file__).resolve())]))
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    try:verify();REPORT["status"]="passed"
    except BaseException as exc:REPORT["status"]="failed";REPORT["error"]=str(exc);print("FAIL "+str(exc),file=sys.stderr,flush=True)
    finally:(OUT/"result.json").write_text(json.dumps(REPORT,indent=2)+"\n")
    raise SystemExit(0 if REPORT["status"]=="passed" else 1)
