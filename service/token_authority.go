package service

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"strings"
)

// TokenAuthority grants query access to one operator-configured index.
// Rotation takes effect when the service restarts.
type TokenAuthority struct {
	digest   [32]byte
	resource string
}

func NewTokenAuthority(path, resource string) (*TokenAuthority, error) {
	if path == "" || resource == "" {
		return nil, errors.New("credential file and resource required")
	}
	before, err := os.Lstat(path)
	if err != nil || !before.Mode().IsRegular() || before.Mode().Perm()&0077 != 0 {
		return nil, errors.New("credential file must be private and regular")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, errors.New("credential unavailable")
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !os.SameFile(before, info) || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("credential changed")
	}
	data, err := io.ReadAll(io.LimitReader(f, 66))
	if err != nil {
		return nil, errors.New("credential unavailable")
	}
	token := strings.TrimSuffix(string(data), "\n")
	raw, err := hex.DecodeString(token)
	if err != nil || len(raw) != 32 || hex.EncodeToString(raw) != token {
		return nil, errors.New("credential must be 64 lowercase hexadecimal characters")
	}
	return &TokenAuthority{sha256.Sum256([]byte(token)), resource}, nil
}

func (a *TokenAuthority) Allow(ctx context.Context, credential, resource, verb string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if resource != a.resource || verb != "query" || !strings.HasPrefix(credential, "Bearer ") {
		return false, nil
	}
	candidate := sha256.Sum256([]byte(strings.TrimPrefix(credential, "Bearer ")))
	return subtle.ConstantTimeCompare(candidate[:], a.digest[:]) == 1, nil
}
