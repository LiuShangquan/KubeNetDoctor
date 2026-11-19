#!/usr/bin/env python3
"""M3：独立内核 K3s 的真实 CNI/Agent、Pod、Service 与 DNS 集成。保留专用演示夹具。"""
import datetime
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import time

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/"build/m3";OUT.mkdir(parents=True,exist_ok=True)
REPORT={"time":datetime.datetime.now(datetime.timezone.utc).isoformat(),"checks":{},"commands":[]}
spec=importlib.util.spec_from_file_location("vmlab",ROOT/"scripts/vm-lab.py")
vm=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm)

def ssh(node,command,content=None,check=True,timeout=45):
    p=subprocess.run(vm.ssh_args(node)+[command],input=content,stdin=subprocess.DEVNULL if content is None else None,text=True,capture_output=True,timeout=timeout)
    REPORT["commands"].append({"node":node,"command":command,"exit":p.returncode,"stdout":p.stdout,"stderr":p.stderr})
    if check and p.returncode:raise RuntimeError(p.stdout+p.stderr)
    return p
def kubectl(*args,**kwargs):return ssh(1,shlex.join(["/usr/local/bin/k3s","kubectl",*args]),**kwargs)
def inside(pod,*args,**kwargs):return kubectl("-n","knd-test","exec",pod,"--",*args,**kwargs)
def passed(name,detail):
    REPORT["checks"][name]={"status":"passed","detail":detail};print("PASS",name,detail,flush=True)
def status(node):return json.loads(ssh(node,"curl --silent --show-error --unix-socket /run/knd/agent.sock http://localhost/v1/status").stdout)
def apply(object):return kubectl("apply","-f","-",content=json.dumps(object))

def verify():
    old=kubectl("get","namespace","knd-test","-o","json",check=False)
    if old.returncode==0:
        assert json.loads(old.stdout)["metadata"].get("labels",{}).get("knd-project")=="integration", "foreign namespace"
    apply({"apiVersion":"v1","kind":"Namespace","metadata":{"name":"knd-test","labels":{"knd-project":"integration"}}})
    manifests=[]
    for name,node in (("a","knd-node1"),("b","knd-node1"),("c","knd-node2")):
        pod={"apiVersion":"v1","kind":"Pod","metadata":{"name":name,"namespace":"knd-test","labels":{"knd-case":name}},
          "spec":{"nodeName":node,"terminationGracePeriodSeconds":2,"containers":[{"name":"tools","image":"docker.io/library/knd-test:v0.1","imagePullPolicy":"Never",
             "command":["sh","-ec","mkdir -p /www; hostname > /www/index.html; python3 -m http.server 8080 --directory /www & socat UDP4-RECVFROM:8081,reuseaddr,fork EXEC:/bin/cat & iperf3 -s -p 5201 > /tmp/iperf.log 2>&1 & wait"],
             "readinessProbe":{"httpGet":{"path":"/","port":8080},"initialDelaySeconds":1,"periodSeconds":2}}]}}
        manifests.append(pod);apply(pod)
    for name,backend in (("local","b"),("remote","c"),("hairpin","a")):
        svc={"apiVersion":"v1","kind":"Service","metadata":{"name":name,"namespace":"knd-test"},"spec":{"selector":{"knd-case":backend},"ports":[
          {"name":"http","port":8080,"targetPort":8080,"protocol":"TCP"},{"name":"echo","port":8081,"targetPort":8081,"protocol":"UDP"},
          {"name":"perf-tcp","port":5201,"targetPort":5201,"protocol":"TCP"},{"name":"perf-udp","port":5201,"targetPort":5201,"protocol":"UDP"}]}}
        manifests.append(svc);apply(svc)
    (ROOT/"deploy/test-fixtures.json").write_text(json.dumps({"apiVersion":"v1","kind":"List","items":manifests},indent=2)+"\n")
    kubectl("-n","knd-test","wait","pod","--all","--for=condition=Ready","--timeout=90s",timeout=100)
    pods=json.loads(kubectl("-n","knd-test","get","pods","-o","json").stdout)["items"]
    by_name={p["metadata"]["name"]:p for p in pods};ips={name:p["status"]["podIP"] for name,p in by_name.items()}
    services=json.loads(kubectl("-n","knd-test","get","svc","-o","json").stdout)["items"]
    vips={s["metadata"]["name"]:s["spec"]["clusterIP"] for s in services}
    evidence=[]
    for node in (1,2):
        state=status(node)
        assert state["localReady"] and state["appliedGeneration"]==state["desiredGeneration"] and not state["lastError"],state
        for record in state["records"].values():
            request=record["request"];pod=request["pod"]
            if pod["namespace"]!="knd-test":continue
            actual=by_name[pod["name"]]
            assert pod["uid"]==actual["metadata"]["uid"] and request["endpoint"]["ip"]==actual["status"]["podIP"]
            assert record["ready"] and request["endpoint"]["netnsInode"]>0
            evidence.append({"pod":pod["name"],"uid":pod["uid"],"ip":request["endpoint"]["ip"],"node":node})
    assert len(evidence)==3
    passed("real_runtime_uid_and_commit",evidence)
    before=status(1)["counters"]
    for destination in ("b","c"):
        assert inside("a","curl","--fail","--silent","--max-time","3",f"http://{ips[destination]}:8080/").stdout.strip()==destination
        assert inside(destination,"curl","--fail","--silent","--max-time","3",f"http://{ips['a']}:8080/").stdout.strip()=="a"
    after=status(1)["counters"]
    if status(1)['mode'].endswith('kernel'):
        assert after['redirect_requested']==before['redirect_requested'],(before,after)
    else:
        assert after["redirect_requested"]>before["redirect_requested"] and after["delivery_fast_seen"]>before["delivery_fast_seen"],(before,after)
    passed("pod_native_baseline_and_local_fast",{"IPs":ips,"localRedirectDelta":after["redirect_requested"]-before["redirect_requested"],"mode":status(1)["mode"]})
    before={n:status(n)["counters"] for n in (1,2)}
    results=[]
    for svc,backend in (("local","b"),("remote","c"),("hairpin","a")):
        assert inside("a","curl","--fail","--silent","--max-time","3",f"http://{vips[svc]}:8080/").stdout.strip()==backend
        token=f"knd-{svc}-udp"
        command=f"printf %s {shlex.quote(token)} | socat -T 2 - UDP4-DATAGRAM:{vips[svc]}:8081"
        assert inside("a","sh","-c",command).stdout==token
        results.append({"service":svc,"VIP":vips[svc],"backend":backend,"TCP":"passed","UDP":"passed"})
    after={n:status(n)["counters"] for n in (1,2)}
    if status(1)['mode'].endswith('kernel'):
        assert after[1]['slow_disabled']>before[1]['slow_disabled']
    else:
        assert after[1]["slow_service"]>before[1]["slow_service"] and after[1]["slow_ct_hit"]>before[1]["slow_ct_hit"]
    passed("clusterip_local_remote_hairpin",results)
    dns=inside("c","dig","+short","local.knd-test.svc.cluster.local").stdout.strip()
    assert vips["local"] in dns
    assert inside("c","curl","--fail","--silent","--max-time","3","http://local.knd-test.svc.cluster.local:8080/").stdout.strip()=="b"
    passed("dns_over_clusterip",{"answer":dns,"source":"remote node Pod"})
    # Agent 停止时内核保留转发；恢复后 UID、ID/epoch 和已挂载程序继续核验。
    prior=status(1);saved={(r["request"]["pod"]["uid"],r["id"],r["epoch"]) for r in prior["records"].values()}
    daemonset=kubectl('-n','knd-system','get','daemonset','knd-agent','-o','json',check=False)
    if daemonset.returncode==0:
        agentpods=json.loads(kubectl('-n','knd-system','get','pods','-l','app=knd-agent','-o','json').stdout)['items']
        victim=next(p for p in agentpods if p['spec']['nodeName']=='knd-node1')
        kubectl('-n','knd-system','delete','pod',victim['metadata']['name'],'--wait=true','--timeout=30s')
        assert inside("a","curl","--fail","--silent","--max-time","3",f"http://{ips['b']}:8080/").stdout.strip()=="b"
        assert inside("a","curl","--fail","--silent","--max-time","3",f"http://{vips['remote']}:8080/").stdout.strip()=="c"
        kubectl('-n','knd-system','rollout','status','daemonset/knd-agent','--timeout=60s',timeout=70)
        replacement=json.loads(kubectl('-n','knd-system','get','pods','-l','app=knd-agent','-o','json').stdout)['items']
        assert victim['metadata']['uid'] not in {p['metadata']['uid'] for p in replacement}
    else:
        ssh(1,"systemctl stop knd-agent")
        try:
            assert inside("a","curl","--fail","--silent","--max-time","3",f"http://{ips['b']}:8080/").stdout.strip()=="b"
            assert inside("a","curl","--fail","--silent","--max-time","3",f"http://{vips['remote']}:8080/").stdout.strip()=="c"
        finally:ssh(1,"systemctl start knd-agent")
    for _ in range(20):
        try:
            current=status(1)
            if current["localReady"]:break
        except (RuntimeError,json.JSONDecodeError):pass
        time.sleep(0.5)
    else:raise RuntimeError("Agent did not recover")
    now={(r["request"]["pod"]["uid"],r["id"],r["epoch"]) for r in current["records"].values()}
    assert saved==now
    assert inside("a","curl","--fail","--silent","--max-time","3",f"http://{ips['b']}:8080/").stdout.strip()=="b"
    passed("agent_restart_preserves_endpoints",{'deployment':'DaemonSet' if daemonset.returncode==0 else 'systemd','endpointIdentities':'preserved','PodIPandVIP':'passed across Agent replacement'})
    REPORT["nodes"]=json.loads(kubectl("get","nodes","-o","json").stdout)["items"]
    REPORT["final_counters"]={n:status(n)["counters"] for n in (1,2)}
    passed("fixtures_retained","knd-test namespace retained for M4 and learning; other clusters untouched")

if __name__=="__main__":
    try:verify();REPORT["status"]="passed"
    except BaseException as exc:
        import traceback
        REPORT["status"]="failed";REPORT["error"]=repr(exc);REPORT['traceback']=traceback.format_exc();print(REPORT['traceback'],flush=True)
    finally:(OUT/"result.json").write_text(json.dumps(REPORT,indent=2)+"\n")
    raise SystemExit(0 if REPORT["status"]=="passed" else 1)
