#!/usr/bin/env python3
"""下载固定版本 K3s 与离线镜像，只接受同一官方 release 的 SHA256。"""
import hashlib
from pathlib import Path
import urllib.request

root=Path(__file__).resolve().parents[1]
cache=root/".cache/k3s";cache.mkdir(parents=True,exist_ok=True)
version="v1.37.2+k3s1"
base="https://github.com/k3s-io/k3s/releases/download/"+version.replace("+","%2B")+"/"
sums=urllib.request.urlopen(base+"sha256sum-amd64.txt",timeout=60).read().decode()
(cache/"sha256sum-amd64.txt").write_text(sums)
for name in ("k3s","k3s-airgap-images-amd64.tar.gz"):
 expected=next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip("*")==name)
 target=cache/name
 def digest(p):
  h=hashlib.sha256()
  with p.open("rb") as f:
   for b in iter(lambda:f.read(4*1024*1024),b""):h.update(b)
  return h.hexdigest()
 if not target.exists() or digest(target)!=expected:
  part=target.with_suffix(target.suffix+".part")
  url="https://rancher-mirror.rancher.cn/k3s/"+version.replace("+","-")+"/"+name
  with urllib.request.urlopen(url,timeout=30) as r,part.open("wb") as f:
   while b:=r.read(256*1024):f.write(b)
  if digest(part)!=expected:raise RuntimeError("checksum mismatch: "+name)
  part.replace(target)
 print("verified",version,name,expected,flush=True)
