#!/usr/bin/env python3
"""导入 Windows Docker 导出的本项目测试镜像到两台 VM 的 containerd。"""
import concurrent.futures
import importlib.util
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("vm",ROOT/"scripts/vm-lab.py")
vm=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm)
def copy(node):
 subprocess.run(["scp","-i",str(vm.KEY),"-P",str(22270+node),"-o","BatchMode=yes","-o","UserKnownHostsFile="+str(vm.WORK/"known_hosts"),str(ROOT/"build/knd-test-v0.1.tar"),"root@127.0.0.1:/opt/knd/test-image.tar"],check=True,timeout=90)
 subprocess.run(vm.ssh_args(node)+["/usr/local/bin/k3s ctr images import /opt/knd/test-image.tar"],check=True,timeout=90)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 for _ in pool.map(copy,(1,2)):pass
