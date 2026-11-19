// SPDX-License-Identifier: GPL-2.0
// 环境探针：验证 TC、Map、FIB 和 CT kfunc 可在真实内核中执行。
// 此程序只观察并放行，不是项目的转发实现或 Service 兼容实现。
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

// 类型从运行内核 BTF 生成，避免不完整 nf_conn 声明导致 kfunc 原型不匹配。
#define ETH_P_IP 0x0800
#define TC_ACT_OK 0
#define TC_ACT_SHOT 2
extern struct nf_conn *bpf_skb_ct_lookup(struct __sk_buff *skb,
    struct bpf_sock_tuple *tuple, __u32 tuple_size,
    struct bpf_ct_opts *opts, __u32 opts_size) __ksym;
extern void bpf_ct_release(struct nf_conn *ct) __ksym;

struct counters {
    __u64 packets;
    __u64 ct_miss;
    __u64 ct_hit;
    __u64 ct_error;
    __u64 fib_calls;
    __u64 fib_success;
};
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct counters);
} env_stats SEC(".maps");

SEC("tc")
int env_observe(struct __sk_buff *skb)
{
    __u32 key = 0;
    struct counters *stats = bpf_map_lookup_elem(&env_stats, &key);
    if (!stats)
        return TC_ACT_SHOT;
    __sync_fetch_and_add(&stats->packets, 1);
    struct ethhdr eth;
    struct iphdr ip;
    if (bpf_skb_load_bytes(skb, 0, &eth, sizeof(eth)) ||
        eth.h_proto != bpf_htons(ETH_P_IP) ||
        bpf_skb_load_bytes(skb, sizeof(eth), &ip, sizeof(ip)))
        return TC_ACT_OK;

    // 人工不存在的 UDP tuple，仅证明查询/引用释放与错误语义可执行。
    struct bpf_sock_tuple tuple = {};
    tuple.ipv4.saddr = ip.saddr;
    tuple.ipv4.daddr = ip.daddr;
    tuple.ipv4.sport = bpf_htons(49123);
    tuple.ipv4.dport = bpf_htons(49124);
    struct bpf_ct_opts opts = {.netns_id = -1, .l4proto = 17};
    struct nf_conn *ct = bpf_skb_ct_lookup(skb, &tuple,
        sizeof(tuple.ipv4), &opts, sizeof(opts));
    if (ct) {
        bpf_ct_release(ct);
        __sync_fetch_and_add(&stats->ct_hit, 1);
    } else if (opts.error == -2) {
        __sync_fetch_and_add(&stats->ct_miss, 1);
    } else {
        __sync_fetch_and_add(&stats->ct_error, 1);
    }

    struct bpf_fib_lookup fib = {};
    fib.family = 2;
    fib.ifindex = skb->ifindex;
    fib.tos = ip.tos;
    fib.l4_protocol = ip.protocol;
    fib.tot_len = bpf_ntohs(ip.tot_len);
    fib.ipv4_src = ip.saddr;
    fib.ipv4_dst = ip.daddr;
    __sync_fetch_and_add(&stats->fib_calls, 1);
    if (bpf_fib_lookup(skb, &fib, sizeof(fib), 0) == BPF_FIB_LKUP_RET_SUCCESS)
        __sync_fetch_and_add(&stats->fib_success, 1);
    return TC_ACT_OK;
}

SEC("tc")
int env_drop(struct __sk_buff *skb)
{
    return TC_ACT_SHOT;
}

char LICENSE[] SEC("license") = "GPL";
