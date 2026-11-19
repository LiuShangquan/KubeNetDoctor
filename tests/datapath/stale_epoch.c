// 测试专用：模拟在查表与最终投递之间发生的附件 epoch 变化，不能部署。
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include "abi.h"
SEC("tc") int test_stale_epoch(struct __sk_buff *skb) {
    skb->cb[0]=KND_CB_MAGIC; skb->cb[1]=2;skb->cb[2]=0;
    skb->cb[3]=0xffffffff;skb->cb[4]=0xffffffff;
    return -1; // TC_ACT_UNSPEC：继续执行后面的正式 egress 程序。
}
char LICENSE[] SEC("license")="GPL";
