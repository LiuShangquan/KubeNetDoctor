.PHONY: build test vet

build:
	mkdir -p build/bin
	CGO_ENABLED=0 go build -trimpath -o build/bin/knd-cni ./cmd/knd-cni
	CGO_ENABLED=0 go build -trimpath -o build/bin/knd-datapath ./cmd/knd-datapath
	CGO_ENABLED=0 go build -trimpath -o build/bin/knd-agent ./cmd/knd-agent
	CGO_ENABLED=0 go build -trimpath -o build/bin/kndctl ./cmd/kndctl
	CGO_ENABLED=0 go build -trimpath -o build/bin/knd-profile ./cmd/knd-profile

bpf: build/bpf/vmlinux.h
	clang -O2 -g -target bpf -D__TARGET_ARCH_x86 -I build/bpf -I bpf -c bpf/datapath.c -o build/bpf/datapath.o
	clang -O2 -g -target bpf -D__TARGET_ARCH_x86 -DKND_BASELINE -I build/bpf -I bpf -c bpf/datapath.c -o build/bpf/datapath.baseline.o

# B3 仅供对照，不会被 Agent 镜像复制或默认加载。
benchmark-bpf: bpf
	clang -O2 -g -target bpf -D__TARGET_ARCH_x86 -DKND_FAST_VERIFIED_CHECKSUM -I build/bpf -I bpf -c bpf/datapath.c -o build/bpf/datapath.optimized.o

build/bpf/vmlinux.h:
	mkdir -p build/bpf
	bpftool btf dump file /sys/kernel/btf/vmlinux format c > $@.tmp
	mv $@.tmp $@

test:
	go test -race ./internal/...

vet:
	go vet ./...
