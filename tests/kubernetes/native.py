#!/usr/bin/env python3
"""M4：Native 跨节点 BPF 路径与真实 kube-proxy Service 门禁。"""
import datetime
import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/os.environ.get('KND_TEST_OUT','build/m4');OUT.mkdir(parents=True,exist_ok=True)
REPORT={"time":datetime.datetime.now(datetime.timezone.utc).isoformat(),"checks":{},"commands":[]}
spec=importlib.util.spec_from_file_location("kube",ROOT/"tests/kubernetes/run.py");k=importlib.util.module_from_spec(spec);spec.loader.exec_module(k)
k.OUT=OUT;k.REPORT=REPORT
ssh,kubectl,inside,status,passed=k.ssh,k.kubectl,k.inside,k.status,k.passed

def raw_packet(source,destination,sport):
    records={r["request"]["pod"]["name"]:r["request"]["endpoint"] for node in (1,2) for r in status(node)["records"].values() if r["request"]["pod"]["namespace"]=="knd-test"}
    a,b=records[source],records[destination]
    packet={"source":a["ip"],"destination":b["ip"],"hostMAC":a["hostMAC"],"podMAC":a["podMAC"],"sport":sport}
    command=shlex.join(["/usr/local/bin/k3s","kubectl","-n","knd-test","exec",destination,"--","python3","/tmp/knd-packet.py","recv",json.dumps(packet)])
    proc=subprocess.Popen(k.vm.ssh_args(1)+[command],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        first=proc.stderr.readline().strip()
        assert first=="READY",f"receiver startup: {first!r}; {proc.stderr.read() if proc.poll() is not None else ''}"
        before={n:status(n)["counters"] for n in (1,2)}
        inside(source,"python3","/tmp/knd-packet.py","send",json.dumps(packet))
        out,err=proc.communicate(timeout=8);assert proc.returncode==0,err
        capture=json.loads(out);assert capture["ttl"]==62 and capture["checksumValid"],capture
        assert capture["srcMAC"]==b["hostMAC"] and capture["dstMAC"]==b["podMAC"]
        after={n:status(n)["counters"] for n in (1,2)}
        for n in (1,2):assert after[n]["redirect_requested"]>before[n]["redirect_requested"] and after[n]["ct_miss"]>before[n]["ct_miss"],(n,before,after)
        target_node=2 if destination=="c" else 1
        assert after[target_node]["delivery_fast_seen"]>before[target_node]["delivery_fast_seen"]
        return {"capture":capture,"redirectDelta":{n:after[n]["redirect_requested"]-before[n]["redirect_requested"] for n in (1,2)}}
    finally:
        if proc.poll() is None:proc.kill();proc.wait()

def verify():
    k.verify() # 重复真实 Pod、Service、hairpin、DNS 与进程恢复验收。
    for n in (1,2):
        s=status(n);assert s["mode"]==os.environ.get('KND_FORWARD_MODE','native-fast') and s["fastCapable"],s
    script=(ROOT/"tests/datapath/packet.py").read_text()
    for pod in ("a","c"):
        encoded=base64.b64encode(script.encode()).decode()
        inside(pod,"sh","-c",f"printf %s {encoded} | base64 -d > /tmp/knd-packet.py")
    # 新 tuple 避免先前慢路径连接的 CT 状态掩盖真实 fast 分支。
    time.sleep(2.2)
    captures=[raw_packet("a","c",40121),raw_packet("c","a",40122)]
    passed("native_two_hop_fast_path",captures)
    services=json.loads(kubectl("-n","knd-test","get","svc","-o","json").stdout)["items"]
    vips={s["metadata"]["name"]:s["spec"]["clusterIP"] for s in services}
    pods=json.loads(kubectl("-n","knd-test","get","pods","-o","json").stdout)["items"]
    ips={p["metadata"]["name"]:p["status"]["podIP"] for p in pods}
    assert vips["local"] in inside("c","dig","+tcp","+short","local.knd-test.svc.cluster.local").stdout
    passed("SVC_02_tcp_dns","UDP DNS was tested above; TCP DNS also succeeds")
    # 持续的旧 Service TCP 连接由内核 CT 保持；新连接选择变更后的后端。
    command=shlex.join(["/usr/local/bin/k3s","kubectl","-n","knd-test","exec","a","--","iperf3","-c",vips["remote"],"-p","5201","-t","8","-R","-J"])
    process=subprocess.Popen(k.vm.ssh_args(1)+[command],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        time.sleep(2)
        kubectl("-n","knd-test","patch","service","remote","--type=merge","-p",json.dumps({"spec":{"selector":{"knd-case":"b"}}}))
        for _ in range(20):
            result=inside("a","curl","--fail","--silent","--max-time","2",f"http://{vips['remote']}:8080/",check=False)
            if result.returncode==0 and result.stdout.strip()=="b":break
            time.sleep(.25)
        else:raise RuntimeError("new Service backend did not converge")
        assert inside("a","curl","--fail","--silent","--max-time","2",f"http://{ips['c']}:8080/").stdout.strip()=="c"
        out,err=process.communicate(timeout=15);assert process.returncode==0,err
        flow=json.loads(out);assert "error" not in flow and flow["end"]["sum_received"]["bytes"]>0,flow
        assert flow["start"]["connected"][0]["remote_host"]==vips["remote"]
        (OUT/"established-service.json").write_text(json.dumps(flow,indent=2))
        passed("SVC_01_05_established_and_backend_change",{"peer":vips["remote"],"bytes":flow["end"]["sum_received"]["bytes"],"newBackend":"b","directPodC":"passed"})
    finally:
        if process.poll() is None:process.kill();process.wait()
        kubectl("-n","knd-test","patch","service","remote","--type=merge","-p",json.dumps({"spec":{"selector":{"knd-case":"c"}}}))
    nodeport={"apiVersion":"v1","kind":"Service","metadata":{"name":"nodeport","namespace":"knd-test"},"spec":{"type":"NodePort","externalTrafficPolicy":"Cluster","selector":{"knd-case":"b"},"ports":[{"name":"http","protocol":"TCP","port":8080,"targetPort":8080,"nodePort":30080},{"name":"echo","protocol":"UDP","port":8081,"targetPort":8081,"nodePort":30081}]}}
    k.apply(nodeport)
    external=f"knd-ext-{os.getpid()}";host=f"kex{os.getpid()}";peer=f"kep{os.getpid()}"
    try:
        ssh(1,f"set -e\nip netns add {external}\nip link add {host} type veth peer name {peer}\nip link set {host} alias knd-test-external\nip link set {peer} netns {external}\nip addr add 198.18.0.1/30 dev {host}\nip link set {host} up\nip netns exec {external} ip link set lo up\nip netns exec {external} ip addr add 198.18.0.2/30 dev {peer}\nip netns exec {external} ip link set {peer} up\nip netns exec {external} ip route add default via 198.18.0.1\n")
        ssh(2,"ip route add 198.18.0.0/30 via 192.168.77.11 dev underlay0 proto 191")
        time.sleep(1)
        response=ssh(1,f"ip netns exec {external} curl --fail --silent --max-time 3 http://192.168.77.12:30080/").stdout.strip();assert response=="b",response
        response=ssh(1,f"ip netns exec {external} python3 -c \"import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);s.sendto(b'nodeport-udp',('192.168.77.12',30081));data,peer=s.recvfrom(256);assert data==b'nodeport-udp';print(peer)\"").stdout
        assert "192.168.77.12" in response and "30081" in response
        logs=kubectl("-n","knd-test","logs","b","--tail=10").stdout
        # MASQUERADE 根据实际输出设备选源地址：Native 为 NodeIP，VXLAN 为 VTEP IP。
        expected_source='10.244.1.0' if status(2)['mode'].startswith('vxlan-') else '192.168.77.12'
        assert expected_source in logs,logs
        passed("SVC_04_nodeport_external_cluster",{"externalClient":"198.18.0.2 in isolated non-Pod namespace","nodePort":"192.168.77.12:30080/30081","backend":"b on node1","backendObservedSource":expected_source})
    finally:
        ssh(1,f"ip netns del {external}",check=False)
        ssh(1,f"ip link del {host}",check=False)
        ssh(2,"ip route del 198.18.0.0/30 via 192.168.77.11 dev underlay0 proto 191",check=False)
    REPORT["final_counters"]={n:status(n)["counters"] for n in (1,2)}
    for counts in REPORT["final_counters"].values():assert counts["drop_mutation"]==0

if __name__=="__main__":
    try:verify();REPORT["status"]="passed"
    except BaseException as exc:
        import traceback
        REPORT["status"]="failed";REPORT["error"]=repr(exc);REPORT["traceback"]=traceback.format_exc();print(REPORT["traceback"],flush=True)
    finally:(OUT/"result.json").write_text(json.dumps(REPORT,indent=2)+"\n")
    raise SystemExit(0 if REPORT["status"]=="passed" else 1)
