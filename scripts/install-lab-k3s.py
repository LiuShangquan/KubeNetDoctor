#!/usr/bin/env python3
"""在专用 KVM 来宾安装 K3s（禁用 Flannel，保留 kube-proxy iptables）。"""
import base64
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("vmlab",ROOT/"scripts/vm-lab.py")
vm=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm)
def ssh(node,script,check=True):
 p=subprocess.run(vm.ssh_args(node)+["bash -s"],input=script,text=True,capture_output=True,timeout=120)
 if check and p.returncode:raise RuntimeError(f"node{node}: {p.stdout}\n{p.stderr}")
 return p
def copy(node,source,target):
 subprocess.run(["scp","-i",str(vm.KEY),"-P",str(22270+node),"-o","BatchMode=yes","-o","UserKnownHostsFile="+str(vm.WORK/"known_hosts"),str(source),"root@127.0.0.1:"+target],check=True,timeout=180,stdout=subprocess.DEVNULL)
def kubectl(*args):
 p=subprocess.run(vm.ssh_args(1)+["/usr/local/bin/k3s","kubectl",*args],text=True,capture_output=True,timeout=30)
 if p.returncode:raise RuntimeError(p.stderr)
 return p.stdout

tokenfile=vm.WORK/"k3s-join-token"
if not tokenfile.exists():tokenfile.write_text(secrets.token_hex(24));tokenfile.chmod(0o600)
token=tokenfile.read_text().strip()
archive=ROOT/".cache/k3s/k3s-airgap-images-amd64.tar.gz"
if not archive.exists():raise RuntimeError("verified airgap image archive missing")

def install(node):
 ssh(node,"set -e\nmkdir -p /var/lib/rancher/k3s/agent/images /etc/rancher/k3s /opt/knd /opt/cni/bin\n")
 copy(node,ROOT/".cache/k3s/k3s","/usr/local/bin/k3s")
 copy(node,archive,"/var/lib/rancher/k3s/agent/images/"+archive.name)
 mode="server" if node==1 else "agent"
 common=f'''node-name: knd-node{node}
node-ip: 192.168.77.{10+node}
token: {token}
'''
 if node==1:
  common+='''advertise-address: 192.168.77.11
cluster-cidr: 10.244.0.0/16
service-cidr: 10.96.0.0/12
cluster-dns: 10.96.0.10
flannel-backend: none
disable-network-policy: true
disable:
  - traefik
  - servicelb
  - local-storage
  - metrics-server
kube-proxy-arg:
  - proxy-mode=iptables
tls-san:
  - 127.0.0.1
  - 192.168.77.11
write-kubeconfig-mode: '0600'
'''
 else:common+='server: https://192.168.77.11:6443\n'
 unit=f'''[Unit]
Description=KubeNetDoctor isolated K3s {mode}
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
ExecStart=/usr/local/bin/k3s {mode} --config /etc/rancher/k3s/config.yaml
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
TimeoutStartSec=0
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
'''
 script="set -e\nchmod 0755 /usr/local/bin/k3s\numask 077\ncat > /etc/rancher/k3s/config.yaml <<'KND_CONFIG'\n"+common+"KND_CONFIG\ncat > /etc/systemd/system/knd-k3s.service <<'KND_UNIT'\n"+unit+"KND_UNIT\nsystemctl daemon-reload\nsystemctl enable --now --no-block knd-k3s\n"
 ssh(node,script)
 return f"node{node} K3s service started"
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 for result in pool.map(install,(1,2)):print(result,flush=True)

deadline=time.monotonic()+180
while time.monotonic()<deadline:
 try:
  nodes=json.loads(kubectl("get","nodes","-o","json"))["items"]
  if len(nodes)==2 and all(n["spec"].get("podCIDR") for n in nodes):break
 except (RuntimeError,json.JSONDecodeError):pass
 time.sleep(2)
else:raise RuntimeError("two Node PodCIDRs were not assigned")
print("API ready and both PodCIDRs assigned",flush=True)

# Agent 使用仅 get/list/watch Node/Pod 的身份，通过节点实际地址引导。
manifest='''apiVersion: v1
kind: Namespace
metadata:
  name: knd-system
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: knd-agent
  namespace: knd-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: knd-agent
rules:
  - apiGroups: [""]
    resources: [nodes, pods]
    verbs: [get, list, watch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: knd-agent
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: knd-agent
subjects:
  - kind: ServiceAccount
    name: knd-agent
    namespace: knd-system
---
apiVersion: v1
kind: Secret
metadata:
  name: knd-agent-token
  namespace: knd-system
  annotations:
    kubernetes.io/service-account.name: knd-agent
type: kubernetes.io/service-account-token
'''
ssh(1,"/usr/local/bin/k3s kubectl apply -f - <<'KND_MANIFEST'\n"+manifest+"KND_MANIFEST\n")
for _ in range(30):
 secret=json.loads(kubectl("-n","knd-system","get","secret","knd-agent-token","-o","json"))
 if secret.get("data",{}).get("token"):break
 time.sleep(1)
else:raise RuntimeError("Agent credential not ready")
data=secret["data"]
kubeconfig={"apiVersion":"v1","kind":"Config","clusters":[{"name":"lab","cluster":{"server":"https://192.168.77.11:6443","certificate-authority-data":data["ca.crt"]}}],
 "users":[{"name":"knd-agent","user":{"token":base64.b64decode(data["token"]).decode()}}],
 "contexts":[{"name":"lab","context":{"cluster":"lab","user":"knd-agent"}}],"current-context":"lab"}
encoded=base64.b64encode(json.dumps(kubeconfig).encode()).decode()
for node in (1,2):
 ssh(node,"set -e\numask 077\nprintf '%s' '"+encoded+"' | base64 -d > /opt/knd/kubeconfig\n")
print("restricted Agent credentials installed; no credential printed",flush=True)
result={"k3s":"v1.37.2+k3s1","cni":"none until knd-agent starts","kubeProxy":"iptables","nodes":[{"name":n["metadata"]["name"],"podCIDR":n["spec"]["podCIDR"]} for n in nodes]}
(ROOT/"build/vms/k3s.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result),flush=True)
