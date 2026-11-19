package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"kubenetdoctor/internal/network"
	"net"
	"net/http"
	"time"
)

type PodIdentity struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	UID       string `json:"uid"`
}
type Request struct {
	APIVersion    string           `json:"apiVersion"`
	Key           string           `json:"key"`
	ContainerID   string           `json:"containerID"`
	ConfigDigest  string           `json:"configDigest"`
	TransactionID string           `json:"transactionID,omitempty"`
	Pod           PodIdentity      `json:"pod"`
	Endpoint      network.Endpoint `json:"endpoint"`
}
type Reply struct {
	TransactionID string `json:"transactionID"`
	ID            uint64 `json:"id"`
	Epoch         uint64 `json:"epoch"`
	Generation    uint64 `json:"generation"`
	Ready         bool   `json:"ready"`
	Error         string `json:"error,omitempty"`
}

// Call 只连接本机 Unix socket，禁用环境 HTTP 代理，限制超时与响应大小。
func Call(socket, operation string, request Request) (Reply, error) {
	var reply Reply
	request.APIVersion = "v1"
	raw, err := json.Marshal(request)
	if err != nil {
		return reply, err
	}
	if len(raw) > 65536 {
		return reply, fmt.Errorf("Agent request exceeds 64 KiB")
	}
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 10 * time.Second}
	response, err := client.Post("http://unix/v1/"+operation, "application/json", bytes.NewReader(raw))
	if err != nil {
		return reply, err
	}
	defer response.Body.Close()
	if err = json.NewDecoder(io.LimitReader(response.Body, 65537)).Decode(&reply); err != nil {
		return reply, err
	}
	if response.StatusCode != http.StatusOK || reply.Error != "" {
		return reply, fmt.Errorf("Agent %s: HTTP %d: %s", operation, response.StatusCode, reply.Error)
	}
	return reply, nil
}
