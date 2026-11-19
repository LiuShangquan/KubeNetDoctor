#!/usr/bin/env python3
"""仅管理本项目两台 KVM 来宾。隔离 socket underlay，不改宿主路由或现有 Docker。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
WORK=Path("/root/.cache/kubenetdoctor-lab")
STATE=ROOT/"build/vms/state.json"
KEY=WORK/"id_ed25519"
def run(cmd,**kwargs):return subprocess.run([str(x) for x in cmd],check=True,**kwargs)
def ssh_args(node):
 return ["ssh","-i",str(KEY),"-p",str(22270+node),"-o","BatchMode=yes","-o","ConnectTimeout=3","-o","StrictHostKeyChecking=accept-new","-o","UserKnownHostsFile="+str(WORK/"known_hosts"),"root@127.0.0.1"]
def up():
 WORK.mkdir(parents=True,exist_ok=True);WORK.chmod(0o700);STATE.parent.mkdir(parents=True,exist_ok=True)
 if STATE.exists():
  old=json.loads(STATE.read_text())
  for item in old.get("nodes",[]):
   cmd=Path(f"/proc/{item['pid']}/cmdline")
   if cmd.exists() and b"knd-lab-node" in cmd.read_bytes():raise RuntimeError("lab already running")
 for port in (22271,22272,26443,15271):
  with socket.socket() as s:s.bind(("127.0.0.1",port))
 if not KEY.exists():run(["ssh-keygen","-t","ed25519","-N","","-C","knd-isolated-lab","-f",KEY],stdout=subprocess.DEVNULL)
 public=KEY.with_suffix(".pub").read_text().strip()
 downloaded=ROOT/".cache/vm/ubuntu-26.04-server-cloudimg-amd64.img"
 if not downloaded.exists():raise RuntimeError("run download-vm-image.py first")
 base=WORK/"ubuntu-base.img"
 if not base.exists():shutil.copyfile(downloaded,base)
 processes=[];logs=[]
 try:
  state={"kernelIsolation":"independent QEMU/KVM guests","nodes":[]}
  for i in (1,2):
   disk=WORK/f"node{i}.qcow2"
   if not disk.exists():
    run(["qemu-img","create","-f","qcow2","-F","qcow2","-b",base,disk,"20G"],stdout=subprocess.DEVNULL)
   userdata=WORK/f"node{i}-user.yaml";metadata=WORK/f"node{i}-meta.yaml";network=WORK/f"node{i}-network.yaml";seed=WORK/f"node{i}-seed.iso"
   userdata.write_text(f'''#cloud-config
hostname: knd-node{i}
manage_etc_hosts: true
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - {public}
write_files:
  - path: /etc/sysctl.d/90-knd-lab.conf
    content: |
      net.ipv4.ip_forward=1
      net.ipv4.conf.all.rp_filter=0
      net.ipv4.conf.default.rp_filter=0
runcmd:
  - [sysctl, --system]
''')
   metadata.write_text(f"instance-id: knd-lab-node{i}\nlocal-hostname: knd-node{i}\n")
   network.write_text(f'''version: 2
ethernets:
  mgmt0:
    match:
      macaddress: "52:54:00:77:0{i}:01"
    set-name: mgmt0
    dhcp4: true
  underlay0:
    match:
      macaddress: "52:54:00:77:0{i}:02"
    set-name: underlay0
    addresses: [192.168.77.{10+i}/24]
    mtu: 1500
''')
   run(["cloud-localds","--network-config",network,seed,userdata,metadata])
   log=(WORK/f"node{i}.console.log").open("ab");logs.append(log)
   usernet=f"user,id=mgmt,hostfwd=tcp:127.0.0.1:{22270+i}-:22"
   if i==1:usernet+=",hostfwd=tcp:127.0.0.1:26443-:6443"
   private="socket,id=underlay,"+("listen" if i==1 else "connect")+"=127.0.0.1:15271"
   cmd=["qemu-system-x86_64","-name",f"knd-lab-node{i}","-enable-kvm","-cpu","host","-smp","2","-m","4096","-nographic","-no-reboot",
     "-drive",f"file={disk},if=virtio,format=qcow2,cache=none","-drive",f"file={seed},if=virtio,format=raw,readonly=on",
     "-netdev",usernet,"-device",f"virtio-net-pci,netdev=mgmt,mac=52:54:00:77:0{i}:01",
     "-netdev",private,"-device",f"virtio-net-pci,netdev=underlay,mac=52:54:00:77:0{i}:02"]
   proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT);processes.append(proc)
   state["nodes"].append({"node":i,"pid":proc.pid,"sshPort":22270+i,"underlayIP":f"192.168.77.{10+i}","log":str(log.name)})
  state["status"]="booting";STATE.write_text(json.dumps(state,indent=2))
  deadline=time.monotonic()+180
  ready=set()
  while time.monotonic()<deadline and len(ready)<2:
   for i,p in enumerate(processes,1):
    if p.poll() is not None:raise RuntimeError(f"QEMU node{i} exited; see console")
    if i not in ready and subprocess.run(ssh_args(i)+["true"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:ready.add(i)
   time.sleep(1)
  if len(ready)!=2:raise RuntimeError("guest SSH readiness timeout")
  state["status"]="running";STATE.write_text(json.dumps(state,indent=2));print("two independent guests ready",flush=True)
  while all(p.poll() is None for p in processes):time.sleep(2)
 finally:
  for p in processes:
   if p.poll() is None:p.terminate()
  for p in processes:
   try:p.wait(timeout=10)
   except subprocess.TimeoutExpired:p.kill();p.wait()
  for log in logs:log.close()
  if STATE.exists():
   state=json.loads(STATE.read_text());state["status"]="stopped";STATE.write_text(json.dumps(state,indent=2))

def main():
 parser=argparse.ArgumentParser();parser.add_argument("command",choices=["up","ssh","copy","status","down"]);parser.add_argument("args",nargs="*");args=parser.parse_args()
 if args.command=="up":
  signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()));up()
 elif args.command=="status":print(STATE.read_text() if STATE.exists() else "not started")
 elif args.command=="ssh":raise SystemExit(subprocess.call(ssh_args(int(args.args[0]))+args.args[1:]))
 elif args.command=="copy":
  i=int(args.args[0]);raise SystemExit(subprocess.call(["scp","-i",str(KEY),"-P",str(22270+i),"-o","BatchMode=yes","-o","UserKnownHostsFile="+str(WORK/"known_hosts"),args.args[1],"root@127.0.0.1:"+args.args[2]]))
 elif args.command=="down":
  for item in json.loads(STATE.read_text())["nodes"]:
   path=Path(f"/proc/{item['pid']}/cmdline")
   if path.exists() and f"knd-lab-node{item['node']}".encode() in path.read_bytes():os.kill(item["pid"],signal.SIGTERM)
if __name__=="__main__":main()
