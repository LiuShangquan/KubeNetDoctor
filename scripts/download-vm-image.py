#!/usr/bin/env python3
"""下载独立内核实验镜像；任何镜像字节都必须匹配 Ubuntu 官方 SHA256。"""
import hashlib
from pathlib import Path
import urllib.request

root=Path(__file__).resolve().parents[1]
cache=root/".cache/vm";cache.mkdir(parents=True,exist_ok=True)
name="ubuntu-26.04-server-cloudimg-amd64.img"
official="https://cloud-images.ubuntu.com/releases/resolute/release/"
sums=urllib.request.urlopen(official+"SHA256SUMS",timeout=30).read().decode()
expected=next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip("*")==name)
(cache/"SHA256SUMS.ubuntu").write_text(sums)
target=cache/name
def digest(path):
 h=hashlib.sha256()
 with path.open("rb") as f:
  for chunk in iter(lambda:f.read(4*1024*1024),b""):h.update(chunk)
 return h.hexdigest()
if not target.exists() or digest(target)!=expected:
 part=cache/(name+".part")
 url="https://mirror.nju.edu.cn/ubuntu-cloud-images/releases/resolute/release/"+name
 with urllib.request.urlopen(url,timeout=60) as response,part.open("wb") as out:
  while chunk:=response.read(4*1024*1024):out.write(chunk)
 if digest(part)!=expected:raise RuntimeError("image checksum mismatch; refused to use download")
 part.replace(target)
print("verified",name,expected,flush=True)
