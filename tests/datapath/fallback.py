#!/usr/bin/env python3
"""模拟内核缺少 CT kfunc 的加载失败；证明基础门禁可独立加载且不快转。"""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/"build/fallback";OUT.mkdir(parents=True,exist_ok=True)
REPORT={"time":datetime.datetime.now(datetime.timezone.utc).isoformat(),"checks":{},"commands":[]}
spec=importlib.util.spec_from_file_location("m2",ROOT/"tests/datapath/run.py");m2=importlib.util.module_from_spec(spec);spec.loader.exec_module(m2)
m2.OUT=OUT;m2.REPORT=REPORT;m2.m0.OUT=OUT;m2.m0.REPORT=REPORT
class Lab(m2.Lab):
 def __init__(self):super().__init__();self.object=OUT/"missing.o"
def verify():
 m2.run(["clang","-O2","-g","-target","bpf","-D__TARGET_ARCH_x86","-Dbpf_skb_ct_lookup=knd_missing_ct_lookup","-I",ROOT/"build/bpf","-I",ROOT/"bpf","-c",ROOT/"bpf/datapath.c","-o",OUT/"missing.o"])
 shutil.copyfile(ROOT/"build/bpf/datapath.baseline.o",OUT/"missing.baseline.o")
 with Lab() as lab:
  config=json.loads(m2.run(["bpftool","-j","map","lookup","pinned",lab.pin/"knd/runtime_config","key","hex","00","00","00","00"]).stdout)["formatted"]["value"]
  assert config["features"]==0 and config["fast_ready"]==0,config
  lab.transfer(5211,"10.244.1.3",udp=True)
  for protocol in ("tcp","udp"):
   m2.ns(lab.node,"iptables","-t","nat","-A","PREROUTING","-d","10.96.0.100","-p",protocol,"-j","DNAT","--to-destination","10.244.1.3")
  lab.transfer(5212,"10.96.0.100",reverse=True)
  lab.transfer(5213,"10.96.0.100",udp=True)
  before=lab.stats();lab.packet(capture=False,podMAC="02:00:00:99:99:99");after=lab.stats()
  assert after["redirect_requested"]==0 and after["slow_disabled"]>0 and after["drop_source"]>before["drop_source"]
  m2.passed("missing_kfunc_baseline",{"configuration":config,"counters":after,"traffic":"PodIP UDP/TCP control, VIP TCP reverse and UDP passed; source gate retained"})
 m2.passed("cleanup","all isolated resources removed")
if __name__=="__main__":
 if os.environ.get("KND_FALLBACK_PRIVATE")!="1":raise SystemExit(subprocess.call(["unshare","--mount","--propagation","private","env","KND_FALLBACK_PRIVATE=1",sys.executable,str(Path(__file__).resolve())]))
 try:verify();REPORT["status"]="passed"
 except BaseException as exc:REPORT["status"]="failed";REPORT["error"]=str(exc);print("FAIL",str(exc),flush=True)
 finally:(OUT/"result.json").write_text(json.dumps(REPORT,indent=2)+"\n")
 raise SystemExit(0 if REPORT["status"]=="passed" else 1)
