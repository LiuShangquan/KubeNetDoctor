// SPDX-License-Identifier: GPL-2.0
// 正式 TC 数据面：只加速已登记 IPv4 TCP/UDP；Service 与 CT 交给 Linux。
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include "abi.h"
#define OK 0
#define DROP 2
#define ETHLEN 14
#ifndef KND_BASELINE
extern struct nf_conn *bpf_skb_ct_lookup(struct __sk_buff *, struct bpf_sock_tuple *,
    __u32, struct bpf_ct_opts *, __u32) __ksym;
extern void bpf_ct_release(struct nf_conn *) __ksym;
#endif

struct snapshot_map {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, KND_CAPACITY);
    __type(key, struct knd_key);
    __type(value, struct knd_value);
};
struct snapshot_map snapshot_template SEC(".maps");
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY_OF_MAPS);
    __uint(max_entries, 1);
    __type(key, __u32);
    __array(values, struct snapshot_map);
} active_snapshot SEC(".maps") = {.values = {[0] = &snapshot_template}};
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, __u32);
    __type(value, struct knd_value);
} bindings SEC(".maps");
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct knd_config);
} runtime_config SEC(".maps");
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, KND_REASON_COUNT);
    __type(key, __u32);
    __type(value, __u64);
} reason_counters SEC(".maps");

static __always_inline int action(__u32 reason, int verdict)
{
    __u64 *p = bpf_map_lookup_elem(&reason_counters, &reason);
    if (p) (*p)++;
    return verdict;
}
static __always_inline int mac_equal(const __u8 *a, const __u8 *b)
{
    return a[0]==b[0] && a[1]==b[1] && a[2]==b[2] &&
           a[3]==b[3] && a[4]==b[4] && a[5]==b[5];
}
static __always_inline int ready(const struct knd_value *v)
{
    return v && (v->flags & (KND_LOCAL|KND_READY|KND_BLOCKED)) == (KND_LOCAL|KND_READY);
}
static __always_inline int same(const struct knd_value *a, const struct knd_value *b)
{
    return a && b && a->id == b->id && a->epoch == b->epoch &&
        a->ifindex == b->ifindex && a->ip == b->ip;
}
static __always_inline struct knd_value *endpoint(void *snapshot, __u32 ip)
{
    struct knd_key key = {.kind=KND_ENDPOINT, .a=ip};
    return bpf_map_lookup_elem(snapshot, &key);
}
static __always_inline int denied(void *snapshot, struct iphdr *ip)
{
    struct knd_key key = {.kind=KND_DENY, .a=ip->saddr, .b=ip->daddr, .protocol=ip->protocol};
    return bpf_map_lookup_elem(snapshot, &key) != 0;
}
// 基本头部验证不依赖 skb 线性区域，合法 options/分片留给协议栈。
static __always_inline int parse_ip(struct __sk_buff *skb, struct iphdr *ip, int verified)
{
    if (bpf_skb_load_bytes(skb, ETHLEN, ip, sizeof(*ip))) return -1;
    __u32 header = (__u32)ip->ihl * 4;
    __u32 length = bpf_ntohs(ip->tot_len);
    if (ip->version != 4 || header < 20 || length < header ||
        skb->len < ETHLEN + length || (ip->frag_off & bpf_htons(0x8000))) return -1;
    // 实验构建才复用可信 fast 标记的校验结果，正式构建始终独立校验。
#ifndef KND_FAST_VERIFIED_CHECKSUM
    verified=0;
#endif
    if (ip->ihl == 5 && !verified) {
        __u16 *words = (__u16 *)ip;
        __u32 sum = 0;
#pragma unroll
        for (int i=0; i<10; i++) sum += words[i];
        sum = (sum & 0xffff) + (sum >> 16);
        sum = (sum & 0xffff) + (sum >> 16);
        if (sum != 0xffff) return -1;
    }
    return 0;
}
struct arp4 { __be16 hardware, protocol; __u8 hlen, plen; __be16 op; __u8 sha[6]; __be32 sip; __u8 tha[6]; __be32 tip; } __attribute__((packed));
static __always_inline int check_arp(struct __sk_buff *skb, struct knd_value *binding, int source)
{
    struct arp4 arp;
    if (bpf_skb_load_bytes(skb, ETHLEN, &arp, sizeof(arp)) ||
        arp.hardware != bpf_htons(1) || arp.protocol != bpf_htons(0x0800) || arp.hlen != 6 || arp.plen != 4 ||
        (arp.op != bpf_htons(1) && arp.op != bpf_htons(2))) return action(DROP_PARSE, DROP);
    if (source && (arp.sip != binding->ip || !mac_equal(arp.sha, binding->pod_mac))) return action(DROP_SOURCE, DROP);
    if (!source && arp.tip != binding->ip) return action(DROP_ENDPOINT, DROP);
    return action(SLOW_PROTOCOL, OK);
}

static __always_inline struct knd_value *node_path(void *snapshot,__u32 id)
{
    struct knd_key key={.kind=KND_NODE,.a=id};
    return bpf_map_lookup_elem(snapshot,&key);
}
static __always_inline int mac_unknown(const __u8 *mac)
{
    return !(mac[0]|mac[1]|mac[2]|mac[3]|mac[4]|mac[5]);
}
// 每个入口获取一次完整快照；本地附件校验与网络入口范围分别验证。
static __always_inline int forward(struct __sk_buff *skb,int from_network)
{
    // 仅本次直接 redirect 使用 cb；慢路径不携带可被误认为快路径的标记。
    skb->cb[0]=0;
    __u32 index = skb->ifindex, zero = 0;
    struct knd_config *cfg=bpf_map_lookup_elem(&runtime_config,&zero);
    if (from_network && (!cfg || cfg->abi!=KND_ABI || !cfg->mode || index!=cfg->ingress_ifindex)) return OK;
    struct knd_value *binding=0;
    if (!from_network) {
        binding=bpf_map_lookup_elem(&bindings,&index);
        if (!ready(binding)) return action(DROP_ENDPOINT,DROP);
    }
    struct ethhdr eth;
    if (bpf_skb_load_bytes(skb, 0, &eth, sizeof(eth))) return action(DROP_PARSE, DROP);
    if (!from_network && binding) {
        if (!mac_equal(eth.h_source,binding->pod_mac)) return action(DROP_SOURCE,DROP);
        if (eth.h_proto==bpf_htons(0x0806)) return check_arp(skb,binding,1);
    }
    if (eth.h_proto!=bpf_htons(0x0800)) return from_network ? OK : action(DROP_PARSE,DROP);
    struct iphdr ip;
    if (parse_ip(skb,&ip,0)) return from_network ? OK : action(DROP_PARSE,DROP);
    if (!from_network && binding && ip.saddr!=binding->ip) return action(DROP_SOURCE,DROP);
    void *snapshot = bpf_map_lookup_elem(&active_snapshot, &zero);
    if (!cfg || cfg->abi!=KND_ABI || !snapshot) return from_network ? OK : action(DROP_ENDPOINT,DROP);
    if (denied(snapshot, &ip)) return action(DROP_ACL, DROP);
    if (!cfg->fast_ready) return action(SLOW_DISABLED, OK);
#ifdef KND_BASELINE
    return action(SLOW_DISABLED,OK);
#else
    if (skb->pkt_type!=0 || (eth.h_dest[0]&1)) return action(SLOW_L2,OK);
    if ((ip.daddr & cfg->service_mask) == cfg->service_network) return action(SLOW_SERVICE, OK);
    if (ip.ihl != 5 || (ip.frag_off & bpf_htons(0x3fff))) return action(SLOW_OPTIONS_FRAGMENT, OK);
    if (ip.ttl <= 1) return action(SLOW_TTL, OK);
    if (skb->gso_segs > 1 || skb->len > cfg->mtu + ETHLEN) return action(SLOW_OFFLOAD, OK);
    struct knd_value *src = endpoint(snapshot, ip.saddr), *dst = endpoint(snapshot, ip.daddr);
    if (!src || !dst) return action(SLOW_UNKNOWN,OK);
    if (from_network) {
        // 不加速宿主业务，也不作为远端到远端的中转器。
        if ((src->flags&KND_LOCAL) || !(dst->flags&KND_LOCAL)) return action(SLOW_UNKNOWN,OK);
        struct knd_value *peer=node_path(snapshot,src->node_id);
        if (!peer || peer->ifindex!=index) return action(SLOW_UNKNOWN,OK);
        if (mac_unknown(peer->pod_mac)) return action(SLOW_NEIGH,OK);
        if (!mac_equal(eth.h_source,peer->pod_mac)) return action(DROP_SOURCE,DROP);
        if (!mac_equal(eth.h_dest,peer->host_mac)) return action(SLOW_L2,OK);
    } else {
        if (!same(src,binding)) return action(SLOW_UNKNOWN,OK);
        if (binding && !mac_equal(eth.h_dest,binding->host_mac)) return action(SLOW_L2,OK);
    }
    struct knd_value *path=dst;
    if (dst->flags&KND_LOCAL) {
        __u32 target_index=dst->ifindex;
        struct knd_value *target=bpf_map_lookup_elem(&bindings,&target_index);
        if (!ready(target) || !same(dst,target)) return action(SLOW_BINDING,OK);
    } else {
        if (!cfg->mode || from_network) return action(SLOW_UNKNOWN,OK);
        path=node_path(snapshot,dst->node_id);
        if (!path || path->ifindex!=cfg->ingress_ifindex) return action(SLOW_UNKNOWN,OK);
        if (mac_unknown(path->pod_mac)) return action(SLOW_NEIGH,OK);
    }
    __u32 out=path->ifindex;
    struct bpf_sock_tuple tuple = {};
    tuple.ipv4.saddr=ip.saddr; tuple.ipv4.daddr=ip.daddr;
    __u32 payload = bpf_ntohs(ip.tot_len) - 20;
    if (ip.protocol == 6) {
        struct tcphdr tcp;
        if (payload < sizeof(tcp) || bpf_skb_load_bytes(skb,ETHLEN+20,&tcp,sizeof(tcp)) ||
            tcp.doff < 5 || (__u32)tcp.doff*4 > payload) return action(DROP_PARSE,DROP);
        tuple.ipv4.sport=tcp.source; tuple.ipv4.dport=tcp.dest;
    } else if (ip.protocol == 17) {
        struct udphdr udp;
        if (payload < sizeof(udp) || bpf_skb_load_bytes(skb,ETHLEN+20,&udp,sizeof(udp)) ||
            bpf_ntohs(udp.len) < 8 || bpf_ntohs(udp.len) > payload) return action(DROP_PARSE,DROP);
        tuple.ipv4.sport=udp.source; tuple.ipv4.dport=udp.dest;
    } else return action(SLOW_PROTOCOL,OK);
    struct bpf_ct_opts opts = {.netns_id=-1, .l4proto=ip.protocol};
    struct nf_conn *ct = bpf_skb_ct_lookup(skb,&tuple,sizeof(tuple.ipv4),&opts,sizeof(opts));
    if (ct) { bpf_ct_release(ct); return action(SLOW_CT_HIT,OK); }
    if (opts.error != -2) return action(SLOW_CT_ERROR,OK);
    action(CT_MISS,OK);
    struct bpf_fib_lookup fib = {};
    fib.family=2; fib.ifindex=index; fib.ipv4_src=ip.saddr; fib.ipv4_dst=ip.daddr;
    fib.tos=ip.tos; fib.l4_protocol=ip.protocol; fib.sport=tuple.ipv4.sport; fib.dport=tuple.ipv4.dport;
    fib.tot_len=bpf_ntohs(ip.tot_len);
    int result=bpf_fib_lookup(skb,&fib,sizeof(fib),0);
    if (result == BPF_FIB_LKUP_RET_NO_NEIGH) return action(SLOW_NEIGH,OK);
    if (result == BPF_FIB_LKUP_RET_FRAG_NEEDED) return action(SLOW_MTU,OK);
    if (result != BPF_FIB_LKUP_RET_SUCCESS || fib.ifindex != out || fib.ipv4_dst!=path->ip ||
        !mac_equal(fib.dmac,path->pod_mac) || !mac_equal(fib.smac,path->host_mac)) return action(SLOW_FIB,OK);
    // 此点之前不写报文；此点之后任何失败都 DROP，避免二次 TTL 或半改写包。
    __u8 ttl=ip.ttl-1;
    __be16 old=bpf_htons(((__u16)ip.ttl<<8)|ip.protocol), next=bpf_htons(((__u16)ttl<<8)|ip.protocol);
    struct {__u8 dst[6], src[6];} mac;
    __builtin_memcpy(mac.dst,fib.dmac,6); __builtin_memcpy(mac.src,fib.smac,6);
    if (bpf_l3_csum_replace(skb,ETHLEN+10,old,next,2) ||
        bpf_skb_store_bytes(skb,ETHLEN+8,&ttl,1,0) || bpf_skb_store_bytes(skb,0,&mac,sizeof(mac),0)) return action(DROP_MUTATION,DROP);
    if (dst->flags&KND_LOCAL) {
        skb->cb[0]=KND_CB_MAGIC;
        skb->cb[1]=(__u32)dst->id; skb->cb[2]=(__u32)(dst->id>>32);
        skb->cb[3]=(__u32)dst->epoch; skb->cb[4]=(__u32)(dst->epoch>>32);
    }
    action(REDIRECT_REQUESTED,OK);
    return bpf_redirect(out,0);
#endif
}

SEC("tc") int knd_from_pod(struct __sk_buff *skb) { return forward(skb,0); }
SEC("tc") int knd_from_network(struct __sk_buff *skb) { return forward(skb,1); }

SEC("tc")
int knd_to_pod(struct __sk_buff *skb)
{
    __u32 index=skb->ifindex, zero=0;
    struct knd_value *binding=bpf_map_lookup_elem(&bindings,&index);
    if (!ready(binding)) return action(DROP_ENDPOINT,DROP);
    if (skb->cb[0]==KND_CB_MAGIC &&
        (((__u64)skb->cb[2]<<32|skb->cb[1])!=binding->id ||
         ((__u64)skb->cb[4]<<32|skb->cb[3])!=binding->epoch)) return action(DROP_ENDPOINT,DROP);
    int was_fast = skb->cb[0]==KND_CB_MAGIC;
    skb->cb[0]=0;
    struct ethhdr eth;
    if (bpf_skb_load_bytes(skb,0,&eth,sizeof(eth))) return action(DROP_PARSE,DROP);
    if (eth.h_proto == bpf_htons(0x0806)) return check_arp(skb,binding,0);
    if (eth.h_proto != bpf_htons(0x0800)) return action(DROP_PARSE,DROP);
    struct iphdr ip;
    if (parse_ip(skb,&ip,was_fast)) return action(DROP_PARSE,DROP);
    if (ip.daddr != binding->ip || !mac_equal(eth.h_dest,binding->pod_mac)) return action(DROP_ENDPOINT,DROP);
    void *snapshot=bpf_map_lookup_elem(&active_snapshot,&zero);
    if (!snapshot) return action(DROP_ENDPOINT,DROP);
    if (denied(snapshot,&ip)) return action(DROP_ACL,DROP);
    if (was_fast) action(DELIVERY_FAST_SEEN,OK);
    return action(DELIVERY_SEEN,OK);
}
char LICENSE[] SEC("license")="GPL";
