/* SPDX-License-Identifier: GPL-2.0 */
#ifndef KND_ABI_H
#define KND_ABI_H
#define KND_ABI 2
#define KND_CAPACITY 4096
#define KND_LOCAL 1
#define KND_READY 2
#define KND_BLOCKED 4
#define KND_ENDPOINT 1
#define KND_NODE 2
#define KND_DENY 3
#define KND_META 4
#define KND_CB_MAGIC 0x4b4e4431

/* IP 字段保存网络序字节；其他数值使用宿主序。固定宽度、显式 padding。 */
struct knd_key { __u32 kind, a, b, protocol; };
struct knd_value {
    __u64 id, epoch;
    __u32 ip, ifindex, node_id, flags;
    __u8 host_mac[6], pod_mac[6];
    __u32 pad;
    __u64 reserved[2];
};
struct knd_config {
    __u32 abi, fast_ready, mode, ingress_ifindex;
    __u32 service_network, service_mask, mtu, features;
};
enum knd_reason {
    REDIRECT_REQUESTED, DELIVERY_SEEN, CT_MISS,
    SLOW_DISABLED, SLOW_SERVICE, SLOW_CT_HIT, SLOW_CT_ERROR, SLOW_UNKNOWN,
    SLOW_NEIGH, SLOW_MTU, SLOW_TTL, SLOW_OPTIONS_FRAGMENT, SLOW_OFFLOAD,
    SLOW_FIB, SLOW_PROTOCOL, SLOW_L2, SLOW_BINDING,
    DROP_SOURCE, DROP_ENDPOINT, DROP_ACL, DROP_PARSE, DROP_MUTATION, DELIVERY_FAST_SEEN,
    KND_REASON_COUNT
};
_Static_assert(sizeof(struct knd_key) == 16, "key ABI");
_Static_assert(sizeof(struct knd_value) == 64, "value ABI");
_Static_assert(sizeof(struct knd_config) == 32, "config ABI");
#endif
