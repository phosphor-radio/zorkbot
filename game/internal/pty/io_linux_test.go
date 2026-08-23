//go:build linux

package pty

import (
	"os"

	"github.com/creack/pty"
)

func openPTYPair() (*os.File, *os.File, error) {
	return pty.Open()
}
