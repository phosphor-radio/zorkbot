//go:build linux

package pty

import (
	"os"
	"syscall"
	"unsafe"
)

func ptyBytesAvailable(f *os.File) (int, error) {
	var n int32

	conn, err := f.SyscallConn()
	if err != nil {
		return 0, err
	}

	var sysErr error
	err = conn.Control(func(fd uintptr) {
		_, _, errno := syscall.Syscall(
			syscall.SYS_IOCTL,
			fd,
			syscall.TIOCINQ,
			uintptr(unsafe.Pointer(&n)),
		)
		if errno != 0 {
			sysErr = errno
		}
	})
	if err != nil {
		return 0, err
	}
	if sysErr != nil {
		return 0, sysErr
	}
	return int(n), nil
}
