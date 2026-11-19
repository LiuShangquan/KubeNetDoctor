#!/usr/bin/env python3
"""部署项目 Agent DaemonSet；先验证专用 VM，再将管理权从 systemd 交给 Kubernetes。"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('k',ROOT/'tests/kubernetes/run.py');k=importlib.util.module_from_spec(spec);spec.loader.exec_module(k)
parser=argparse.ArgumentParser();parser.add_argument('--mode',default='vxlan-fast',choices=['native-fast','vxlan-fast']);parser.add_argument('--tag',default='v0.3');args=parser.parse_args()
if not args.tag.replace('.','').isalnum():raise RuntimeError('invalid image tag')
for n in (1,2):
    s=k.status(n)
    if not s['localReady'] or s['mode']!=args.mode:raise RuntimeError('validate systemd mode before transfer')
    p=ROOT/('build/knd-agent-'+args.tag+'.tar')
    subprocess.run(['scp','-i',str(k.vm.KEY),'-P',str(22270+n),'-o','BatchMode=yes','-o','UserKnownHostsFile='+str(k.vm.WORK/'known_hosts'),str(p),f'root@127.0.0.1:/opt/knd/agent-image.tar'],check=True,stdin=subprocess.DEVNULL)
    k.ssh(n,'/usr/local/bin/k3s ctr images import /opt/knd/agent-image.tar',timeout=90)

rbac=[{'apiVersion':'v1','kind':'ServiceAccount','metadata':{'name':'knd-agent','namespace':'knd-system'}},
 {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'ClusterRole','metadata':{'name':'knd-agent'},'rules':[{'apiGroups':[''],'resources':['nodes','pods'],'verbs':['get','list','watch']}]},
 {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'ClusterRoleBinding','metadata':{'name':'knd-agent'},'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':'knd-agent'},'subjects':[{'kind':'ServiceAccount','name':'knd-agent','namespace':'knd-system'}]}]
volumes=[];mounts=[]
for name,path,kind in [('bpf','/sys/fs/bpf','Directory'),('state','/var/lib/knd','Directory'),('ipam','/var/lib/cni/networks','Directory'),('socket','/run/knd','DirectoryOrCreate'),('config','/etc/cni/net.d','Directory'),('netns','/var/run/netns','DirectoryOrCreate')]:
    volumes.append({'name':name,'hostPath':{'path':path,'type':kind}})
    mount={'name':name,'mountPath':path}
    if name=='netns':mount['mountPropagation']='HostToContainer'
    mounts.append(mount)
volumes.append({'name':'bin','hostPath':{'path':'/opt/cni/bin','type':'Directory'}})
image='docker.io/library/knd-agent:'+args.tag
ds={'apiVersion':'apps/v1','kind':'DaemonSet','metadata':{'name':'knd-agent','namespace':'knd-system','labels':{'app':'knd-agent'}},'spec':{'selector':{'matchLabels':{'app':'knd-agent'}},'updateStrategy':{'type':'RollingUpdate','rollingUpdate':{'maxUnavailable':1}},'template':{'metadata':{'labels':{'app':'knd-agent'}},'spec':{
 'serviceAccountName':'knd-agent','hostNetwork':True,'hostPID':True,'dnsPolicy':'Default','terminationGracePeriodSeconds':10,'tolerations':[{'operator':'Exists'}],
 'initContainers':[{'name':'install-cni','image':image,'imagePullPolicy':'Never','command':['sh','-ec','for name in knd-cni host-local loopback; do cp /opt/knd/cni/$name /host/cni/$name.new; chmod 0755 /host/cni/$name.new; mv /host/cni/$name.new /host/cni/$name; done'],'volumeMounts':[{'name':'bin','mountPath':'/host/cni'}]}],
 'containers':[{'name':'agent','image':image,'imagePullPolicy':'Never','args':['-underlay','underlay0','-mode',args.mode],
  'env':[{'name':'NODE_NAME','valueFrom':{'fieldRef':{'fieldPath':'spec.nodeName'}}},{'name':'KUBERNETES_SERVICE_HOST','value':'192.168.77.11'},{'name':'KUBERNETES_SERVICE_PORT','value':'6443'}],
  'securityContext':{'privileged':True},'volumeMounts':mounts,'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'memory':'512Mi'}},
  'startupProbe':{'exec':{'command':['kndctl','doctor']},'periodSeconds':2,'failureThreshold':60},'readinessProbe':{'exec':{'command':['kndctl','doctor']},'periodSeconds':3,'failureThreshold':3}}],
 'volumes':volumes}}}}
manifest={'apiVersion':'v1','kind':'List','items':rbac+[ds]}
(ROOT/'deploy/lab-daemonset.json').write_text(json.dumps(manifest,indent=2)+'\n')
for n in (1,2):k.ssh(n,'systemctl disable --now knd-agent')
k.apply(manifest)
k.kubectl('-n','knd-system','rollout','status','daemonset/knd-agent','--timeout=120s',timeout=130)
for n in (1,2):
    state=k.status(n);assert state['localReady'] and not state['lastError'],state
print('DaemonSet ready on both nodes; systemd Agent disabled',flush=True)
