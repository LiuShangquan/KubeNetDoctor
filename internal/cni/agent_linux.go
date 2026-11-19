//go:build linux

package cni

import (
	"fmt"
	"github.com/containernetworking/cni/pkg/skel"
	"github.com/containernetworking/cni/pkg/types"
	"kubenetdoctor/internal/api"
)

type runtimeArgs struct {
	types.CommonArgs
	K8S_POD_NAME               types.UnmarshallableString
	K8S_POD_NAMESPACE          types.UnmarshallableString
	K8S_POD_UID                types.UnmarshallableString
	K8S_POD_INFRA_CONTAINER_ID types.UnmarshallableString
}

func podIdentity(args *skel.CmdArgs) (api.PodIdentity, error) {
	var k runtimeArgs
	if err := types.LoadArgs(args.Args, &k); err != nil {
		return api.PodIdentity{}, err
	}
	p := api.PodIdentity{Namespace: string(k.K8S_POD_NAMESPACE), Name: string(k.K8S_POD_NAME), UID: string(k.K8S_POD_UID)}
	if p.Namespace == "" || p.Name == "" || p.UID == "" {
		return p, fmt.Errorf("runtime must provide K8S_POD_NAMESPACE, K8S_POD_NAME and K8S_POD_UID")
	}
	return p, nil
}
func agentRequest(a *Attachment) api.Request {
	return api.Request{Key: a.Key, ContainerID: a.ContainerID, ConfigDigest: a.ConfigDigest, TransactionID: a.TransactionID, Pod: a.Pod, Endpoint: a.Endpoint}
}

// ReapDetached 由恢复扫描调用：只清理已经断开、IPAM 已释放、Agent 记录也已撤销的 tombstone。
func ReapDetached(dir string, removed func(string) bool) error {
	s, err := openStore(dir)
	if err != nil {
		return err
	}
	defer s.close()
	entries, err := s.list()
	if err != nil {
		return err
	}
	for _, a := range entries {
		if a.Phase == "detached" && removed(a.Key) {
			if err = s.remove(a.Key); err != nil {
				return err
			}
		}
	}
	return nil
}
