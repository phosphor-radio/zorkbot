package pty

import (
	"testing"
	"time"
)

func TestDrainPTYDiscardsPendingBytes(t *testing.T) {
	master, slave, err := openPTYPair()
	if err != nil {
		t.Fatalf("open pty: %v", err)
	}
	t.Cleanup(func() {
		_ = master.Close()
		_ = slave.Close()
	})

	session := &Session{
		ptmx: master,
		cfg:  Config{IdleWait: defaultIdleWait},
	}

	if _, err := slave.Write([]byte("leftover")); err != nil {
		t.Fatalf("write slave: %v", err)
	}
	time.Sleep(20 * time.Millisecond)

	session.drainPTY()

	avail, err := ptyBytesAvailable(master)
	if err != nil {
		t.Fatalf("ptyBytesAvailable: %v", err)
	}
	if avail > 0 {
		t.Fatalf("expected drained PTY to be empty, %d bytes remain", avail)
	}
}
