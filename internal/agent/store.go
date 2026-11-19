package agent

import (
	"encoding/json"
	"fmt"
	"kubenetdoctor/internal/api"
	"kubenetdoctor/internal/network"
	"os"
	"path/filepath"
)

type Record struct {
	Request       api.Request `json:"request"`
	ID            uint64      `json:"id"`
	Epoch         uint64      `json:"epoch"`
	TransactionID string      `json:"transactionID"`
	Ready         bool        `json:"ready"`
	Deleting      bool        `json:"deleting"`
}
type State struct {
	Schema     int                 `json:"schema"`
	BootID     string              `json:"bootID"`
	NodeUID    string              `json:"nodeUID"`
	NextID     uint64              `json:"nextID"`
	Generation uint64              `json:"generation"`
	Records    map[string]*Record  `json:"records"`
	Routes     []network.NodeRoute `json:"routes"`
	Policy     Policy              `json:"policy"`
	Mode       string              `json:"mode,omitempty"`
	VXLANPeers []network.VXLANPeer `json:"vxlanPeers,omitempty"`
}

func load(path string) (State, error) {
	s := State{Schema: 1, Records: map[string]*Record{}}
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return s, nil
	}
	if err != nil {
		return s, err
	}
	err = json.Unmarshal(raw, &s)
	if err != nil {
		return s, err
	}
	if s.Schema != 1 || s.Records == nil {
		return s, fmt.Errorf("invalid Agent state schema")
	}
	if err = s.Policy.Validate(); err != nil {
		return s, err
	}
	return s, nil
}
func save(path string, s State) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".agent-txn-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	_, err = f.Write(append(raw, '\n'))
	if err == nil {
		err = f.Sync()
	}
	ce := f.Close()
	if err != nil {
		return err
	}
	if ce != nil {
		return ce
	}
	if err = os.Rename(f.Name(), path); err != nil {
		return err
	}
	d, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
