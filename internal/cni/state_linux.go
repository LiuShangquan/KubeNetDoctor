//go:build linux

package cni

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"kubenetdoctor/internal/api"
	"kubenetdoctor/internal/network"

	current "github.com/containernetworking/cni/pkg/types/100"
	"golang.org/x/sys/unix"
)

// Attachment 是跨进程恢复记录；必须在分配 IP 和创建接口之前先落盘。
type Attachment struct {
	network.Endpoint
	Schema        int             `json:"schema"`
	Key           string          `json:"key"`
	ContainerID   string          `json:"containerID"`
	Phase         string          `json:"phase"`
	ConfigDigest  string          `json:"configDigest"`
	RawConfig     json.RawMessage `json:"rawConfig"`
	Result        *current.Result `json:"result,omitempty"`
	Pod           api.PodIdentity `json:"pod,omitempty"`
	TransactionID string          `json:"transactionID,omitempty"`
}

// store 用一把节点级锁串行化附件变更，避免每个附件永久残留一份 lock 文件。
type store struct {
	dir  string
	lock *os.File
}

func openStore(dir string) (*store, error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("stateDir must be a real directory")
	}
	fd, err := unix.Open(filepath.Join(dir, ".lock"), unix.O_CREAT|unix.O_RDWR|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), "knd-state-lock")
	if err := unix.Flock(fd, unix.LOCK_EX); err != nil {
		f.Close()
		return nil, err
	}
	return &store{dir: dir, lock: f}, nil
}

func (s *store) close() { _ = unix.Flock(int(s.lock.Fd()), unix.LOCK_UN); _ = s.lock.Close() }

func (s *store) list() ([]*Attachment, error) {
	files, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, err
	}
	var result []*Attachment
	for _, f := range files {
		if !f.IsDir() && strings.HasSuffix(f.Name(), ".json") {
			key := strings.TrimSuffix(f.Name(), ".json")
			if len(key) != 64 {
				continue
			}
			a, err := s.read(key)
			if err != nil {
				return nil, err
			}
			result = append(result, a)
		}
	}
	return result, nil
}

func (s *store) read(key string) (*Attachment, error) {
	data, err := os.ReadFile(filepath.Join(s.dir, key+".json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var a Attachment
	if err := json.Unmarshal(data, &a); err != nil {
		return nil, fmt.Errorf("invalid attachment journal: %w", err)
	}
	if a.Schema != 1 || a.Key != key {
		return nil, fmt.Errorf("attachment schema/key mismatch")
	}
	return &a, nil
}

// write 先 fsync 临时文件，再 rename 并 fsync 目录，使阶段记录可恢复。
func (s *store) write(a *Attachment) error {
	data, err := json.MarshalIndent(a, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(s.dir, ".txn-")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err = f.Write(append(data, '\n')); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err := os.Rename(name, filepath.Join(s.dir, a.Key+".json")); err != nil {
		return err
	}
	return s.syncDir()
}

func (s *store) remove(key string) error {
	err := os.Remove(filepath.Join(s.dir, key+".json"))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return s.syncDir()
}

func (s *store) syncDir() error {
	f, err := os.Open(s.dir)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func namespaceInode(path string) (uint64, error) {
	var stat unix.Stat_t
	if err := unix.Stat(path, &stat); err != nil {
		return 0, err
	}
	return stat.Ino, nil
}
