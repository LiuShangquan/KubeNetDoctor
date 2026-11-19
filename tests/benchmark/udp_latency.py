#!/usr/bin/env python3
"""性能测量夹具：持久 UDP echo，记录含 Python/调度开销的端到端 RTT。"""
import json
import socket
import sys
import time

mode,address,port=sys.argv[1],sys.argv[2],int(sys.argv[3])
sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.settimeout(3)
if mode=='server':
    sock.bind((address,port));print('READY',file=sys.stderr,flush=True)
    while True:
        try:data,peer=sock.recvfrom(2048);sock.sendto(data,peer)
        except socket.timeout:continue
else:
    sock.connect((address,port));samples=[]
    for i in range(2100):
        data=i.to_bytes(8,'big')+b'x'*56;start=time.monotonic_ns();sock.send(data);assert sock.recv(2048)==data
        if i>=100:samples.append((time.monotonic_ns()-start)/1000)
    ordered=sorted(samples)
    print(json.dumps({'count':len(samples),'unit':'microseconds','p50':ordered[int(.5*(len(samples)-1))],'p95':ordered[int(.95*(len(samples)-1))],'p99':ordered[int(.99*(len(samples)-1))],'samples':samples}))
