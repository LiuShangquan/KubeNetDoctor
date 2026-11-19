package datapath

import (
	"bytes"
	"encoding/binary"
	"kubenetdoctor/internal/network"
	"testing"
	"unsafe"
)

func fixture() Snapshot {
	return Snapshot{Generation: 1, MTU: 1450, ServiceCIDR: "10.96.0.0/12", FastReady: true, Endpoints: []Endpoint{
		{Endpoint: network.Endpoint{IP: "10.244.1.2", HostIndex: 7, HostName: "kn-test", Owner: "knd:test", HostMAC: "02:00:00:00:00:01", PodMAC: "02:00:00:00:00:02"}, ID: 42, Epoch: 99, Ready: true},
	}}
}
func TestABI(t *testing.T) {
	if unsafe.Sizeof(Key{}) != 16 || unsafe.Sizeof(Value{}) != 64 || unsafe.Sizeof(Config{}) != 32 {
		t.Fatal("ABI layout mismatch")
	}
	v, err := Compile(fixture())
	if err != nil {
		t.Fatal(err)
	}
	ip, _ := IPWord("10.244.1.2")
	buf := new(bytes.Buffer)
	if err = binary.Write(buf, binary.NativeEndian, v.Entries[Key{Kind: EndpointKind, A: ip}]); err != nil {
		t.Fatal(err)
	}
	b := buf.Bytes()
	if len(b) != 64 || !bytes.Equal(b[16:20], []byte{10, 244, 1, 2}) || binary.NativeEndian.Uint64(b[:8]) != 42 || binary.NativeEndian.Uint64(b[8:16]) != 99 || b[32] != 2 || b[38] != 2 {
		t.Fatalf("ABI bytes %x", b)
	}
}
func TestRejectAmbiguousSnapshot(t *testing.T) {
	for _, alter := range []func(*Snapshot){
		func(s *Snapshot) { s.Endpoints = append(s.Endpoints, s.Endpoints[0]) },
		func(s *Snapshot) { s.Endpoints[0].Epoch = 0 },
		func(s *Snapshot) { s.Endpoints[0].IP = "10.96.1.2" },
		func(s *Snapshot) { s.Endpoints[0].PodMAC = "ff:ff:ff:ff:ff:ff" },
		func(s *Snapshot) { s.ServiceCIDR = "0.0.0.0/0" },
		func(s *Snapshot) { s.Deny = []Deny{{SourceID: 42, DestinationID: 500, Protocol: 6}} },
	} {
		s := fixture()
		alter(&s)
		if _, err := Compile(s); err == nil {
			t.Fatal("ambiguous snapshot accepted")
		}
	}
}
