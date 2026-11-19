//go:build linux

package agent

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
	core "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/typed/core/v1"
	listers "k8s.io/client-go/listers/core/v1"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/tools/clientcmd"
	"kubenetdoctor/internal/api"
	"kubenetdoctor/internal/cni"
	"kubenetdoctor/internal/datapath"
	"kubenetdoctor/internal/network"
)

type Options struct {
	NodeName, Underlay, ServiceCIDR, Mode, Kubeconfig, Socket, StateDir, CNIStateDir, CNIConfigDir, IPAMDir, PinDir, Object string
	MTU                                                                                                                     int
}
type Agent struct {
	mu           sync.Mutex
	options      Options
	client       *v1.CoreV1Client
	pods         listers.PodLister
	nodes        listers.NodeLister
	dp           *datapath.Manager
	state        State
	node         network.NodeConfig
	localUID     string
	lastError    string
	applied      uint64
	localReady   bool
	lastAPI      time.Time
	apiReachable bool
	lastAPIProbe time.Time
	apiError     string
	queue        chan struct{}
	paths        []datapath.NodePath
	remote       []datapath.RemoteEndpoint
	ingressIndex uint32
}

func (a *Agent) persist() error {
	return save(filepath.Join(a.options.StateDir, "agent.json"), a.state)
}
func (a *Agent) enqueue() {
	select {
	case a.queue <- struct{}{}:
	default:
	}
}

// Run 在宿主网络启动 informer 与本地协议；数据包不经过此进程。
func Run(ctx context.Context, o Options) error {
	if o.NodeName == "" || (o.Mode != "kernel" && o.Mode != "local-fast" && o.Mode != "native-fast" && o.Mode != "vxlan-kernel" && o.Mode != "vxlan-fast") {
		return fmt.Errorf("node name and supported forwarding mode required")
	}
	if err := os.MkdirAll(o.StateDir, 0700); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(o.StateDir, "agent.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = unix.Flock(int(lock.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		return fmt.Errorf("another Agent owns the state directory: %w", err)
	}
	var cfg *rest.Config
	if o.Kubeconfig != "" {
		cfg, err = clientcmd.BuildConfigFromFlags("", o.Kubeconfig)
	} else {
		cfg, err = rest.InClusterConfig()
	}
	if err != nil {
		return err
	}
	cfg.QPS = 10
	cfg.Burst = 20
	cfg.Timeout = 15 * time.Second
	client, err := v1.NewForConfig(cfg)
	if err != nil {
		return err
	}
	a := &Agent{options: o, client: client, queue: make(chan struct{}, 1)}
	a.state, err = load(filepath.Join(o.StateDir, "agent.json"))
	if err != nil {
		return err
	}
	podInformer := cache.NewSharedIndexInformer(&cache.ListWatch{
		ListFunc:  func(opts metav1.ListOptions) (runtime.Object, error) { return client.Pods("").List(ctx, opts) },
		WatchFunc: func(opts metav1.ListOptions) (watch.Interface, error) { return client.Pods("").Watch(ctx, opts) },
	}, &core.Pod{}, 30*time.Second, cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc})
	nodeInformer := cache.NewSharedIndexInformer(&cache.ListWatch{
		ListFunc:  func(opts metav1.ListOptions) (runtime.Object, error) { return client.Nodes().List(ctx, opts) },
		WatchFunc: func(opts metav1.ListOptions) (watch.Interface, error) { return client.Nodes().Watch(ctx, opts) },
	}, &core.Node{}, 30*time.Second, cache.Indexers{})
	handler := cache.ResourceEventHandlerFuncs{AddFunc: func(any) { a.enqueue() }, UpdateFunc: func(any, any) { a.enqueue() }, DeleteFunc: func(any) { a.enqueue() }}
	if _, err = podInformer.AddEventHandler(handler); err != nil {
		return err
	}
	if _, err = nodeInformer.AddEventHandler(handler); err != nil {
		return err
	}
	a.pods = listers.NewPodLister(podInformer.GetIndexer())
	a.nodes = listers.NewNodeLister(nodeInformer.GetIndexer())
	go podInformer.Run(ctx.Done())
	go nodeInformer.Run(ctx.Done())
	if !cache.WaitForCacheSync(ctx.Done(), podInformer.HasSynced, nodeInformer.HasSynced) {
		return fmt.Errorf("Kubernetes cache sync failed")
	}
	local, err := a.nodes.Get(o.NodeName)
	if err != nil {
		return err
	}
	if len(local.Spec.PodCIDRs) != 1 {
		return fmt.Errorf("one IPv4 Node PodCIDR required")
	}
	podRange, err := netip.ParsePrefix(local.Spec.PodCIDR)
	if err != nil || !podRange.Addr().Is4() {
		return fmt.Errorf("invalid local PodCIDR")
	}
	service, err := netip.ParsePrefix(o.ServiceCIDR)
	if err != nil || !service.Addr().Is4() || service.Overlaps(podRange) {
		return fmt.Errorf("invalid or overlapping ServiceCIDR")
	}
	link, err := netlink.LinkByName(o.Underlay)
	if err != nil {
		return err
	}
	addresses, err := netlink.AddrList(link, netlink.FAMILY_V4)
	if err != nil {
		return err
	}
	ip := nodeIP(local)
	found := false
	for _, address := range addresses {
		if address.IP.Equal(net.ParseIP(ip)) {
			found = true
		}
	}
	if !found {
		return fmt.Errorf("Node InternalIP must belong to configured underlay")
	}
	a.node = network.NodeConfig{Name: "knd", PodCIDR: local.Spec.PodCIDR, Gateway: "169.254.1.1", NodeIP: ip, MTU: o.MTU}
	a.localUID = string(local.UID)
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return err
	}
	bootID := strings.TrimSpace(string(boot))
	if a.state.NodeUID != "" && a.state.NodeUID != a.localUID {
		return fmt.Errorf("Node UID changed; existing state requires explicit recovery")
	}
	if a.state.BootID != "" && a.state.BootID != bootID {
		a.state.Records = map[string]*Record{}
		a.state.Routes = nil
	}
	a.state.BootID = bootID
	a.state.NodeUID = a.localUID
	if a.state.Mode != "" && a.state.Mode != o.Mode && len(a.state.Records) != 0 {
		return fmt.Errorf("mode change requires all local attachments removed")
	}
	a.state.Mode = o.Mode
	if err = network.PrepareNode(a.node); err != nil {
		return err
	}
	a.dp, err = datapath.Open(o.PinDir, o.Object)
	if err != nil {
		return err
	}
	defer a.dp.Close()
	if err = a.reconcileLocked(ctx); err != nil {
		return err
	}
	if err = a.writeCNI(); err != nil {
		return err
	}
	a.localReady = true
	a.lastAPI = time.Now()
	a.apiReachable = true
	a.lastAPIProbe = time.Now()
	// API 连通性探测不持写锁；断连保留内核状态，新附件仍须 fresh GET 核验。
	go func() {
		tick := time.NewTicker(5 * time.Second)
		defer tick.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
			}
			call, cancel := context.WithTimeout(ctx, 2*time.Second)
			_, e := client.Nodes().Get(call, o.NodeName, metav1.GetOptions{})
			cancel()
			a.mu.Lock()
			a.apiReachable = e == nil
			a.lastAPIProbe = time.Now()
			a.apiError = ""
			if e != nil {
				a.apiError = e.Error()
			}
			a.mu.Unlock()
		}
	}()
	if err = os.MkdirAll(filepath.Dir(o.Socket), 0700); err != nil {
		return err
	}
	if info, e := os.Lstat(o.Socket); e == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return fmt.Errorf("socket path occupied by non-socket")
		}
		if err = os.Remove(o.Socket); err != nil {
			return err
		}
	}
	listener, err := net.Listen("unix", o.Socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	defer os.Remove(o.Socket)
	if err = os.Chmod(o.Socket, 0600); err != nil {
		return err
	}
	server := &http.Server{Handler: a, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, MaxHeaderBytes: 8192,
		ConnContext: func(ctx context.Context, conn net.Conn) context.Context {
			u, ok := conn.(*net.UnixConn)
			if !ok {
				return context.WithValue(ctx, peerKey{}, false)
			}
			raw, e := u.SyscallConn()
			root := false
			if e == nil {
				_ = raw.Control(func(fd uintptr) {
					cred, e := unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
					root = e == nil && cred.Uid == 0
				})
			}
			return context.WithValue(ctx, peerKey{}, root)
		}}
	errorsCh := make(chan error, 1)
	go func() { errorsCh <- server.Serve(listener) }()
	defer server.Close()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	log.Printf("Agent ready: node=%s CIDR=%s mode=%s", o.NodeName, a.node.PodCIDR, o.Mode)
	for {
		select {
		case <-ctx.Done():
			return nil
		case err = <-errorsCh:
			if errors.Is(err, http.ErrServerClosed) {
				return nil
			}
			return err
		case <-ticker.C:
		case <-a.queue:
		}
		a.mu.Lock()
		err = a.reconcileLocked(ctx)
		if err != nil {
			a.lastError = err.Error()
			log.Printf("reconcile: %v", err)
		} else {
			a.lastError = ""
		}
		keys := map[string]bool{}
		for key := range a.state.Records {
			keys[key] = true
		}
		a.mu.Unlock()
		// 不持 Agent mutex 等待 CNI flock，避免 CNI 持锁调用 UDS 时形成反向死锁。
		if err = cni.ReapDetached(o.CNIStateDir, func(key string) bool { return !keys[key] }); err != nil {
			log.Printf("tombstone sweep: %v", err)
		}
	}
}

type peerKey struct{}

func nodeIP(node *core.Node) string {
	for _, a := range node.Status.Addresses {
		if a.Type == core.NodeInternalIP && net.ParseIP(a.Address).To4() != nil {
			return a.Address
		}
	}
	return ""
}

func (a *Agent) writeCNI() error {
	prefix := netip.MustParsePrefix(a.node.PodCIDR)
	start := prefix.Addr().Next().Next()
	conf := map[string]any{"cniVersion": "1.0.0", "name": "knd", "type": "knd-cni", "podCIDR": a.node.PodCIDR, "gateway": a.node.Gateway, "mtu": a.node.MTU, "stateDir": a.options.CNIStateDir, "agentSocket": a.options.Socket,
		"ipam": map[string]any{"type": "host-local", "dataDir": a.options.IPAMDir, "ranges": [][]map[string]string{{{"subnet": a.node.PodCIDR, "rangeStart": start.String()}}}}}
	if err := os.MkdirAll(a.options.CNIConfigDir, 0755); err != nil {
		return err
	}
	path := filepath.Join(a.options.CNIConfigDir, "10-knd.conf")
	raw, err := json.MarshalIndent(conf, "", "  ")
	if err != nil {
		return err
	}
	// 固定项目文件名；拒绝覆盖同名外部 CNI。
	if old, e := os.ReadFile(path); e == nil {
		var v map[string]any
		if json.Unmarshal(old, &v) != nil || v["name"] != "knd" || v["type"] != "knd-cni" {
			return fmt.Errorf("foreign CNI configuration at %s", path)
		}
	}
	tmp := path + ".tmp"
	if err = os.WriteFile(tmp, append(raw, '\n'), 0644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (a *Agent) reconcileLocked(ctx context.Context) error {
	nodes, err := a.nodes.List(labels.Everything())
	if err != nil {
		return err
	}
	var routes []network.NodeRoute
	var paths []datapath.NodePath
	var vxPeers []network.VXLANPeer
	nodeIDs := map[string]uint32{}
	nodeCIDRs := map[string]netip.Prefix{}
	link, err := netlink.LinkByName(a.options.Underlay)
	if err != nil {
		return err
	}
	a.ingressIndex = uint32(link.Attrs().Index)
	neighbors, err := netlink.NeighList(link.Attrs().Index, netlink.FAMILY_V4)
	if err != nil {
		return err
	}
	localPrefix := netip.MustParsePrefix(a.node.PodCIDR)
	var vxlink netlink.Link
	if strings.HasPrefix(a.options.Mode, "vxlan-") {
		vxlink, err = network.PrepareVXLAN(link, a.localUID, a.node.NodeIP, localPrefix.Addr().String(), a.options.MTU)
		if err != nil {
			return err
		}
		a.ingressIndex = uint32(vxlink.Attrs().Index)
	}
	service := netip.MustParsePrefix(a.options.ServiceCIDR)
	seen := []netip.Prefix{localPrefix}
	for _, node := range nodes {
		if node.Name == a.options.NodeName {
			if string(node.UID) != a.localUID || node.Spec.PodCIDR != a.node.PodCIDR {
				return fmt.Errorf("local node identity/CIDR changed")
			}
			continue
		}
		if node.DeletionTimestamp != nil || node.Spec.PodCIDR == "" {
			continue
		}
		p, e := netip.ParsePrefix(node.Spec.PodCIDR)
		if e != nil || !p.Addr().Is4() || p.Overlaps(service) {
			return fmt.Errorf("invalid remote PodCIDR")
		}
		for _, old := range seen {
			if p.Overlaps(old) {
				return fmt.Errorf("overlapping Node PodCIDRs")
			}
		}
		seen = append(seen, p)
		ip := nodeIP(node)
		if ip == "" {
			continue
		}
		lookup, e := netlink.RouteGet(net.ParseIP(ip))
		if e != nil || len(lookup) != 1 {
			return fmt.Errorf("remote Node IP not routable")
		}
		link, e := netlink.LinkByName(a.options.Underlay)
		if e != nil {
			return e
		}
		if lookup[0].LinkIndex != link.Attrs().Index || (vxlink == nil && lookup[0].Gw != nil) {
			return fmt.Errorf("native baseline requires direct underlay neighbor")
		}
		routes = append(routes, network.NodeRoute{CIDR: p.String(), Gateway: ip, IfIndex: link.Attrs().Index})
		hash := sha256.Sum256([]byte(node.UID))
		id := binary.LittleEndian.Uint32(hash[:4]) | 1
		peerMAC := "00:00:00:00:00:00"
		for _, neighbor := range neighbors {
			if neighbor.IP.Equal(net.ParseIP(ip)) && len(neighbor.HardwareAddr) == 6 && neighbor.State&(netlink.NUD_INCOMPLETE|netlink.NUD_FAILED) == 0 {
				peerMAC = neighbor.HardwareAddr.String()
			}
		}
		paths = append(paths, datapath.NodePath{ID: id, IP: ip, IfIndex: uint32(link.Attrs().Index), LocalMAC: link.Attrs().HardwareAddr.String(), PeerMAC: peerMAC})
		if vxlink != nil {
			peer := network.VXLANPeer{VTEP: p.Addr().String(), MAC: network.VTEPMAC(string(node.UID)).String(), NodeIP: ip}
			if peer.MAC == vxlink.Attrs().HardwareAddr.String() {
				return fmt.Errorf("local/remote VTEP MAC collision")
			}
			vxPeers = append(vxPeers, peer)
			routes[len(routes)-1] = network.NodeRoute{CIDR: p.String(), Gateway: peer.VTEP, IfIndex: vxlink.Attrs().Index, Onlink: true}
			paths[len(paths)-1] = datapath.NodePath{ID: id, IP: peer.VTEP, IfIndex: uint32(vxlink.Attrs().Index), LocalMAC: vxlink.Attrs().HardwareAddr.String(), PeerMAC: peer.MAC}
		}
		nodeIDs[node.Name] = id
		nodeCIDRs[node.Name] = p
	}
	if vxlink != nil {
		if err = network.EnsureVXLANPeers(vxlink, vxPeers); err != nil {
			return err
		}
	}
	if err = network.ReconcileNodeRoutes(routes, a.state.Routes); err != nil {
		return err
	}
	if vxlink != nil {
		if err = network.PruneVXLANPeers(vxlink, vxPeers, a.state.VXLANPeers); err != nil {
			return err
		}
	} else if err = network.RemoveVXLAN(a.localUID); err != nil {
		return err
	}
	a.state.VXLANPeers = vxPeers
	a.state.Routes = routes
	pods, err := a.pods.List(labels.Everything())
	if err != nil {
		return err
	}
	var remote []datapath.RemoteEndpoint
	for _, pod := range pods {
		id, exists := nodeIDs[pod.Spec.NodeName]
		if !exists || pod.Spec.HostNetwork || pod.DeletionTimestamp != nil || pod.Status.Phase == core.PodSucceeded || pod.Status.Phase == core.PodFailed || pod.Status.PodIP == "" {
			continue
		}
		ip, e := netip.ParseAddr(pod.Status.PodIP)
		if e != nil || !ip.Is4() || !nodeCIDRs[pod.Spec.NodeName].Contains(ip) {
			return fmt.Errorf("remote Pod IP outside owning Node CIDR")
		}
		hash := sha256.Sum256([]byte(pod.UID))
		token := binary.LittleEndian.Uint64(hash[:8]) | (uint64(1) << 63)
		remote = append(remote, datapath.RemoteEndpoint{UID: string(pod.UID), ID: token, Epoch: token, IP: ip.String(), NodeID: id})
	}
	a.paths = paths
	a.remote = remote
	for key, r := range a.state.Records {
		link, e := netlink.LinkByName(r.Request.Endpoint.HostName)
		if e != nil {
			var missing netlink.LinkNotFoundError
			if errors.As(e, &missing) {
				delete(a.state.Records, key)
				continue
			}
			return e
		}
		ep := r.Request.Endpoint
		if link.Attrs().Index != ep.HostIndex || link.Attrs().Alias != ep.Owner || link.Attrs().HardwareAddr.String() != ep.HostMAC {
			if err := a.dp.BlockBinding(uint32(ep.HostIndex), r.ID, r.Epoch); err != nil {
				return fmt.Errorf("ownership drift; block failed: %w", err)
			}
			r.Ready = false
			if err := a.persist(); err != nil {
				return err
			}
			return fmt.Errorf("registered interface ownership changed; old binding blocked and verified")
		}
		pod, e := a.pods.Pods(r.Request.Pod.Namespace).Get(r.Request.Pod.Name)
		if e != nil {
			// CNI 可以早于 informer 观察到 Pod；缓存缺失不等于 API 已删除。
			call, cancel := context.WithTimeout(ctx, 3*time.Second)
			pod, e = a.client.Pods(r.Request.Pod.Namespace).Get(call, r.Request.Pod.Name, metav1.GetOptions{})
			cancel()
			if e != nil && !apierrors.IsNotFound(e) {
				continue
			}
		}
		if apierrors.IsNotFound(e) || (e == nil && (string(pod.UID) != r.Request.Pod.UID || pod.DeletionTimestamp != nil || pod.Spec.NodeName != a.options.NodeName || (pod.Status.PodIP != "" && pod.Status.PodIP != ep.IP))) {
			r.Ready = false
			r.Deleting = true
		}
	}
	return a.publishLocked()
}
func (a *Agent) publishLocked() error {
	a.state.Generation++
	snapshot := datapath.Snapshot{Generation: a.state.Generation, ServiceCIDR: a.options.ServiceCIDR, MTU: uint32(a.options.MTU), FastReady: a.options.Mode == "local-fast"}
	snapshot.Paths = a.paths
	snapshot.Remote = a.remote
	snapshot.IngressIfIndex = a.ingressIndex
	if a.options.Mode == "native-fast" {
		snapshot.Mode = 1
		snapshot.FastReady = true
	}
	if strings.HasPrefix(a.options.Mode, "vxlan-") {
		snapshot.Mode = 2
		snapshot.FastReady = a.options.Mode == "vxlan-fast"
	}
	ids := map[string]uint64{}
	for _, remote := range a.remote {
		ids[remote.UID] = remote.ID
	}
	for _, r := range a.state.Records {
		ids[r.Request.Pod.UID] = r.ID
		snapshot.Endpoints = append(snapshot.Endpoints, datapath.Endpoint{Endpoint: r.Request.Endpoint, ID: r.ID, Epoch: r.Epoch, Ready: r.Ready && !r.Deleting && !a.state.Policy.Blocked(r.Request.Pod.UID)})
	}
	snapshot.Deny = a.state.Policy.Resolve(ids)
	if err := a.persist(); err != nil {
		return err
	}
	if err := a.dp.Apply(snapshot); err != nil {
		return err
	}
	for _, r := range a.state.Records {
		if a.state.Policy.Blocked(r.Request.Pod.UID) {
			if err := a.dp.BlockBinding(uint32(r.Request.Endpoint.HostIndex), r.ID, r.Epoch); err != nil {
				return err
			}
		}
	}
	a.applied = a.state.Generation
	return nil
}

func (a *Agent) verifyPod(ctx context.Context, r api.Request) error {
	call, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	pod, err := a.client.Pods(r.Pod.Namespace).Get(call, r.Pod.Name, metav1.GetOptions{})
	if err != nil {
		return err
	}
	a.lastAPI = time.Now()
	if string(pod.UID) != r.Pod.UID || pod.Spec.NodeName != a.options.NodeName || pod.Spec.HostNetwork || pod.DeletionTimestamp != nil {
		return fmt.Errorf("runtime Pod UID/node identity conflict")
	}
	if pod.Status.PodIP != "" && pod.Status.PodIP != r.Endpoint.IP {
		return fmt.Errorf("API PodIP differs from CNI allocation")
	}
	return nil
}
func validRequest(r api.Request) error {
	if r.APIVersion != "v1" || len(r.Key) != 64 || len(r.ConfigDigest) != 64 || r.ContainerID == "" || r.Pod.UID == "" || r.Pod.Namespace == "" || r.Pod.Name == "" {
		return fmt.Errorf("invalid protocol/identity")
	}
	if _, err := hex.DecodeString(r.Key); err != nil {
		return err
	}
	if r.Endpoint.Owner != "knd:v1:"+r.Key || r.Endpoint.HostName != "kn"+r.Key[:11] || r.Key != cni.AttachmentKey("knd", r.ContainerID, r.Endpoint.IfName) {
		return fmt.Errorf("attachment ownership key mismatch")
	}
	return nil
}
func (a *Agent) operate(ctx context.Context, operation string, r api.Request) (api.Reply, error) {
	if err := validRequest(r); err != nil {
		return api.Reply{}, err
	}
	old := a.state.Records[r.Key]
	if old != nil {
		if old.Request.ContainerID != r.ContainerID || old.Request.Pod != r.Pod || old.Request.Endpoint != r.Endpoint || old.Request.ConfigDigest != r.ConfigDigest || (r.TransactionID != "" && r.TransactionID != old.TransactionID) {
			return api.Reply{}, fmt.Errorf("attachment transaction identity conflict")
		}
	}
	reply := func(record *Record) api.Reply {
		if record == nil {
			return api.Reply{}
		}
		return api.Reply{TransactionID: record.TransactionID, ID: record.ID, Epoch: record.Epoch, Ready: record.Ready && !record.Deleting && !a.state.Policy.Blocked(record.Request.Pod.UID), Generation: a.applied}
	}
	switch operation {
	case "prepare":
		if err := a.verifyPod(ctx, r); err != nil {
			return api.Reply{}, err
		}
		if old != nil {
			if old.Deleting {
				return api.Reply{}, fmt.Errorf("attachment is deleting")
			}
			return reply(old), nil
		}
		// 清理已确认链路消失的记录，避免 IP 复用被先前已阻断附件阻塞。
		if err := a.reconcileLocked(ctx); err != nil {
			return api.Reply{}, err
		}
		p, err := netip.ParseAddr(r.Endpoint.IP)
		if err != nil || !netip.MustParsePrefix(a.node.PodCIDR).Contains(p) {
			return api.Reply{}, fmt.Errorf("IP outside local PodCIDR")
		}
		link, err := netlink.LinkByName(r.Endpoint.HostName)
		if err != nil {
			return api.Reply{}, err
		}
		if link.Attrs().Flags&net.FlagUp != 0 {
			return api.Reply{}, fmt.Errorf("new host veth must remain DOWN until prepare")
		}
		id := a.state.NextID + 1
		if id == 0 || id >= uint64(1)<<63 {
			return api.Reply{}, fmt.Errorf("endpoint ID exhausted")
		}
		a.state.NextID = id
		token := make([]byte, 16)
		if _, err = rand.Read(token); err != nil {
			return api.Reply{}, err
		}
		record := &Record{Request: r, ID: id, Epoch: id, TransactionID: hex.EncodeToString(token)}
		a.state.Records[r.Key] = record
		if err = a.publishLocked(); err != nil {
			return api.Reply{}, err
		}
		return reply(record), nil
	case "commit":
		if old == nil || r.TransactionID == "" || old.Deleting {
			return api.Reply{}, fmt.Errorf("prepared transaction required")
		}
		if err := a.verifyPod(ctx, r); err != nil {
			return api.Reply{}, err
		}
		if err := network.ConfigureHost(a.node, r.Endpoint); err != nil {
			return api.Reply{}, err
		}
		if err := network.CheckEndpoint(a.node, r.Endpoint); err != nil {
			return api.Reply{}, err
		}
		old.Ready = true
		if err := a.publishLocked(); err != nil {
			old.Ready = false
			_ = a.persist()
			return api.Reply{}, err
		}
		return reply(old), nil
	case "endpoint":
		if old == nil {
			return api.Reply{}, fmt.Errorf("attachment not registered")
		}
		return reply(old), nil
	case "delete":
		if old == nil {
			return api.Reply{}, nil
		}
		old.Ready = false
		old.Deleting = true
		if err := a.publishLocked(); err != nil {
			return api.Reply{}, err
		}
		return reply(old), nil
	default:
		return api.Reply{}, fmt.Errorf("unknown endpoint operation")
	}
}
func (a *Agent) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if root, _ := r.Context().Value(peerKey{}).(bool); !root {
		http.Error(w, "root peer required", http.StatusForbidden)
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if r.Method == http.MethodGet && r.URL.Path == "/v1/status" {
		counters, err := a.dp.Stats()
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"localReady": a.localReady, "mode": a.options.Mode, "fastCapable": a.dp.FastCapable(), "capabilityError": a.dp.CapabilityError(), "desiredGeneration": a.state.Generation, "appliedGeneration": a.applied, "records": a.state.Records, "policy": a.state.Policy, "routes": a.state.Routes, "paths": a.paths, "remote": a.remote, "lastError": a.lastError, "lastAPIVerified": a.lastAPI, "apiReachable": a.apiReachable, "lastAPIProbe": a.lastAPIProbe, "apiError": a.apiError, "counters": counters})
		return
	}
	if r.Method != http.MethodPost || !strings.HasPrefix(r.URL.Path, "/v1/") {
		http.NotFound(w, r)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 65536)
	defer r.Body.Close()
	if r.URL.Path == "/v1/policy" {
		var policy Policy
		decoder := json.NewDecoder(r.Body)
		decoder.DisallowUnknownFields()
		err := decoder.Decode(&policy)
		if err == nil {
			var extra any
			if decoder.Decode(&extra) != io.EOF {
				err = fmt.Errorf("single JSON document required")
			}
		}
		if err == nil {
			err = policy.Validate()
		}
		if err != nil {
			w.WriteHeader(400)
			_ = json.NewEncoder(w).Encode(api.Reply{Error: err.Error()})
			return
		}
		if policy.APIVersion == "" {
			policy.APIVersion = "v1"
		}
		a.state.Policy = policy
		if err = a.publishLocked(); err != nil {
			a.lastError = err.Error()
			w.WriteHeader(500)
			_ = json.NewEncoder(w).Encode(api.Reply{Error: err.Error()})
			return
		}
		_ = json.NewEncoder(w).Encode(api.Reply{Ready: true, Generation: a.applied})
		return
	}
	var request api.Request
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(api.Reply{Error: err.Error()})
		return
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		w.WriteHeader(400)
		_ = json.NewEncoder(w).Encode(api.Reply{Error: "single JSON document required"})
		return
	}
	reply, err := a.operate(r.Context(), strings.TrimPrefix(r.URL.Path, "/v1/"), request)
	if err != nil {
		a.lastError = err.Error()
		w.WriteHeader(409)
		reply.Error = err.Error()
	}
	_ = json.NewEncoder(w).Encode(reply)
}
