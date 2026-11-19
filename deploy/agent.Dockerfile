FROM alpine:3.23.3
COPY build/bin/knd-agent /usr/local/bin/knd-agent
COPY build/bin/kndctl /usr/local/bin/kndctl
COPY build/bin/knd-cni /opt/knd/cni/knd-cni
COPY .cache/cni/host-local /opt/knd/cni/host-local
COPY .cache/cni/loopback /opt/knd/cni/loopback
COPY build/bpf/datapath.o /opt/knd/datapath.o
COPY build/bpf/datapath.baseline.o /opt/knd/datapath.baseline.o
ENTRYPOINT ["/usr/local/bin/knd-agent"]
