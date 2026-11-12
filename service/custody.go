package service

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sync"
)

var ErrCapacity = errors.New("no execution capacity")

// Custody durably claims capacity before dispatch. Release is valid only after a
// definite completed response, including validation rejections (400 and 413).
// Other engine errors and interrupted calls retain their claim.
type Custody interface{ Claim() (func() error, error) }

// FileCustody is a single-host shared-filesystem adapter. The directory must be
// on persistent storage, private to the service and shared by all local replicas.
// Orphaned slots require independently verified physical stop and operator repair.
type FileCustody struct {
	directory string
	capacity  int
}

func NewFileCustody(directory string, capacity int) (*FileCustody, error) {
	if directory == "" || capacity < 1 || capacity > 64 {
		return nil, errors.New("invalid custody configuration")
	}
	info, err := os.Stat(directory)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("custody directory must be private and pre-created")
	}
	return &FileCustody{directory, capacity}, nil
}

// syncDir flushes directory entries; Windows directory sync remains best effort.
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	syncErr := d.Sync()
	closeErr := d.Close()
	if runtime.GOOS == "windows" {
		syncErr = nil
	}
	return errors.Join(syncErr, closeErr)
}

func (f *FileCustody) Claim() (func() error, error) {
	for i := 0; i < f.capacity; i++ {
		slot := filepath.Join(f.directory, fmt.Sprintf("slot-%02d", i))
		if err := os.Mkdir(slot, 0700); err != nil {
			if os.IsExist(err) {
				continue
			}
			return nil, err
		}
		if err := syncDir(f.directory); err != nil {
			return nil, err
		}
		var once sync.Once
		var releaseErr error
		return func() error {
			once.Do(func() {
				if err := os.Remove(slot); err != nil {
					releaseErr = err
					return
				}
				releaseErr = syncDir(f.directory)
			})
			return releaseErr
		}, nil
	}
	return nil, ErrCapacity
}
