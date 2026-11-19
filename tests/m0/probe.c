// SPDX-License-Identifier: GPL-2.0
// M0 机制探针。固定隔离拓扑，包含故障注入；不能部署为正式数据面。
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#define OK 0
#define DROP 2
#define ETHLEN 14

extern struct nf_conn *bpf_skb_ct_lookup(struct __sk_buff *, struct bpf_sock_tuple *,
    __u32, struct bpf_ct_opts *, __u32) __ksym;
extern void bpf_ct_release(struct nf_conn *) __ksym;

struct config {
    __u32 enabled;
    __u32 ct_mode; // 0 正常；1 注入查询错误；2 禁用保护，仅用于负面对照。
    __u32 drop_ifindex;
    __u32 if_a;
    __u32 if_b;
};
struct counters {
    __u64 redirect_requested;
    __u64 delivery_seen;
    __u64 ct_miss;
    __u64 ct_original;
    __u64 ct_reply;
    __u64 ct_error;
    __u64 slow_other;
    __u64 delivery_drop;
    __u64 mutation_error;
};
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct config);
} m0_config SEC(".maps");
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct counters);
} m0_stats SEC(".maps");
#define COUNT(field) __sync_fetch_and_add(&s->field, 1)

SEC("tc")
int m0_forward(struct __sk_buff *skb)
{
    __u32 zero = 0;
    struct config *cfg = bpf_map_lookup_elem(&m0_config, &zero);
    struct counters *s = bpf_map_lookup_elem(&m0_stats, &zero);
    if (!cfg || !s)
        return DROP;
    struct ethhdr eth;
    struct iphdr ip;
    if (!cfg->enabled || skb->pkt_type != 0 ||
        bpf_skb_load_bytes(skb, 0, &eth, sizeof(eth)) ||
        eth.h_proto != bpf_htons(0x0800) ||
        bpf_skb_load_bytes(skb, ETHLEN, &ip, sizeof(ip)))
        goto slow;
    if (ip.version != 4 || ip.ihl != 5 || ip.ttl <= 1 ||
        (ip.frag_off & bpf_htons(0x3fff)) || skb->len > 1514)
        goto slow;
    struct bpf_sock_tuple tuple = {};
    if (ip.protocol == 6 || ip.protocol == 17) {
        struct { __be16 sport; __be16 dport; } ports;
        if (bpf_skb_load_bytes(skb, ETHLEN + sizeof(ip), &ports, sizeof(ports)))
            return DROP;
        tuple.ipv4.saddr = ip.saddr;
        tuple.ipv4.daddr = ip.daddr;
        tuple.ipv4.sport = ports.sport;
        tuple.ipv4.dport = ports.dport;
        if (cfg->ct_mode != 2) {
            struct bpf_ct_opts opts = {.netns_id = -1, .l4proto = ip.protocol};
            if (cfg->ct_mode == 1)
                opts.netns_id = -2;
            struct nf_conn *ct = bpf_skb_ct_lookup(skb, &tuple,
                sizeof(tuple.ipv4), &opts, sizeof(opts));
            if (ct) {
                bpf_ct_release(ct);
                if (opts.dir == 0)
                    COUNT(ct_original);
                else
                    COUNT(ct_reply);
                return OK;
            }
            if (opts.error != -2) {
                COUNT(ct_error);
                return OK;
            }
            COUNT(ct_miss);
        }
    } else if (ip.protocol != 1) {
        goto slow;
    }
    // M0 特意允许 ICMP 快转，用 TTL 检查 helper 语义；正式实现仍按方案限制 TCP/UDP。
    __u32 src = bpf_ntohl(ip.saddr), dst = bpf_ntohl(ip.daddr);
    if ((src != 0x0af40102 && src != 0x0af40103) ||
        (dst != 0x0af40102 && dst != 0x0af40103))
        goto slow;
    struct bpf_fib_lookup fib = {};
    fib.family = 2;
    fib.ifindex = skb->ifindex;
    fib.ipv4_src = ip.saddr;
    fib.ipv4_dst = ip.daddr;
    fib.tos = ip.tos;
    fib.l4_protocol = ip.protocol;
    fib.sport = tuple.ipv4.sport;
    fib.dport = tuple.ipv4.dport;
    fib.tot_len = bpf_ntohs(ip.tot_len);
    if (bpf_fib_lookup(skb, &fib, sizeof(fib), 0) != BPF_FIB_LKUP_RET_SUCCESS)
        goto slow;
    if (fib.ifindex != (dst == 0x0af40102 ? cfg->if_a : cfg->if_b))
        goto slow;
    __u8 ttl = ip.ttl - 1;
    __be16 old_word = bpf_htons(((__u16)ip.ttl << 8) | ip.protocol);
    __be16 new_word = bpf_htons(((__u16)ttl << 8) | ip.protocol);
    struct { __u8 dst[6]; __u8 src[6]; } mac;
    __builtin_memcpy(mac.dst, fib.dmac, 6);
    __builtin_memcpy(mac.src, fib.smac, 6);
    // 所有可回退检查都在修改之前；修改阶段任何错误均丢弃。
    if (bpf_l3_csum_replace(skb, ETHLEN + 10, old_word, new_word, 2) ||
        bpf_skb_store_bytes(skb, ETHLEN + 8, &ttl, 1, 0) ||
        bpf_skb_store_bytes(skb, 0, &mac, sizeof(mac), 0)) {
        COUNT(mutation_error);
        return DROP;
    }
    COUNT(redirect_requested);
    return bpf_redirect(fib.ifindex, 0);
slow:
    COUNT(slow_other);
    return OK;
}

SEC("tc")
int m0_delivery(struct __sk_buff *skb)
{
    __u32 zero = 0;
    struct config *cfg = bpf_map_lookup_elem(&m0_config, &zero);
    struct counters *s = bpf_map_lookup_elem(&m0_stats, &zero);
    if (!cfg || !s)
        return DROP;
    if (cfg->drop_ifindex == skb->ifindex) {
        COUNT(delivery_drop);
        return DROP;
    }
    COUNT(delivery_seen);
    return OK;
}
char LICENSE[] SEC("license") = "GPL";
