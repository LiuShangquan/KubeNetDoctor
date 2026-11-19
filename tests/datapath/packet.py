#!/usr/bin/env python3
"""协议边界测试用原始 UDP 报文；不作为网络应用或性能负载。"""
import json
import socket
import struct
import sys
import time

def checksum(raw):
    if len(raw)%2: raw+=b"\0"
    n=sum(struct.unpack("!"+"H"*(len(raw)//2),raw))
    while n>>16: n=(n&65535)+(n>>16)
    return (~n)&65535

def send(c):
    mac=lambda x:bytes.fromhex(x.replace(":",""))
    options=bytes.fromhex(c.get("options",""))
    udp=struct.pack("!HHHH",c.get("sport",40001),9000,8+16,0)+b"knd-packet-test!!"
    if c.get("bad_udp"):udp=udp[:4]+struct.pack("!H",65535)+udp[6:]
    if c.get("truncated_tcp"): udp=b"1234"
    proto=6 if c.get("truncated_tcp") else 17
    hdr=struct.pack("!BBHHHBBH4s4s",0x45+len(options)//4,0,20+len(options)+len(udp),1234,c.get("fragment",0),c.get("ttl",64),proto,0,socket.inet_aton(c["source"]),socket.inet_aton(c["destination"]))+options
    cs=checksum(hdr)
    if c.get("bad_checksum"):cs^=1
    hdr=hdr[:10]+struct.pack("!H",cs)+hdr[12:]
    frame=mac(c["hostMAC"])+mac(c["podMAC"])+b"\x08\x00"+hdr+udp
    s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW);s.bind(("eth0",0));s.send(frame);s.close()

def receive(c):
    s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x0800));s.bind(("eth0",0));s.settimeout(3)
    print("READY",file=sys.stderr,flush=True)
    end=time.monotonic()+3
    while time.monotonic()<end:
        raw=s.recv(65535)
        if len(raw)<42:continue
        ihl=(raw[14]&15)*4
        if raw[23]!=17 or socket.inet_ntoa(raw[30:34])!=c["destination"]:continue
        if struct.unpack("!H",raw[14+ihl+2:14+ihl+4])[0]!=9000:continue
        print(json.dumps({"ttl":raw[22],"source":socket.inet_ntoa(raw[26:30]),"destination":socket.inet_ntoa(raw[30:34]),"dstMAC":raw[:6].hex(":"),"srcMAC":raw[6:12].hex(":"),"checksumValid":checksum(raw[14:14+ihl])==0}));return
    raise RuntimeError("capture timeout")

if __name__=="__main__":
    (send if sys.argv[1]=="send" else receive)(json.loads(sys.argv[2]))
