// kndctl 仅连接当前节点的 root Unix socket；不直接改内核表。
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"
)

func run() error {
	socket := flag.String("socket", "/run/knd/agent.sock", "节点 Agent Unix socket")
	flag.Parse()
	args := flag.Args()
	if len(args) < 1 {
		return fmt.Errorf("usage: kndctl [-socket PATH] status|doctor|counters|endpoints|policy FILE")
	}
	command := args[0]
	path := "/v1/status"
	method := http.MethodGet
	var body []byte
	switch command {
	case "status", "doctor", "counters", "endpoints":
		if len(args) != 1 {
			return fmt.Errorf("unexpected arguments")
		}
	case "policy":
		if len(args) != 2 {
			return fmt.Errorf("policy requires a JSON file")
		}
		var err error
		body, err = os.ReadFile(args[1])
		if err != nil {
			return err
		}
		if len(body) > 65536 {
			return fmt.Errorf("policy exceeds 64 KiB")
		}
		path = "/v1/policy"
		method = http.MethodPost
	default:
		return fmt.Errorf("unknown command %q", command)
	}
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", *socket)
	}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 15 * time.Second}
	request, err := http.NewRequest(method, "http://unix"+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 2<<20))
	if err != nil {
		return err
	}
	if response.StatusCode != 200 {
		return fmt.Errorf("Agent HTTP %d: %s", response.StatusCode, raw)
	}
	var data map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err = decoder.Decode(&data); err != nil {
		return err
	}
	var output any = data
	switch command {
	case "counters":
		output = data["counters"]
	case "endpoints":
		output = data["records"]
	case "doctor":
		if data["localReady"] != true || data["lastError"] != "" || data["desiredGeneration"] != data["appliedGeneration"] {
			return fmt.Errorf("Agent not reconciled: %s", raw)
		}
		output = map[string]any{"ready": true, "mode": data["mode"], "fastCapable": data["fastCapable"], "capabilityError": data["capabilityError"], "apiReachable": data["apiReachable"], "apiError": data["apiError"], "generation": data["appliedGeneration"]}
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(output)
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
