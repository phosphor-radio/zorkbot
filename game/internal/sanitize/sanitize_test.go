package sanitize

import "testing"

func TestValidateAllowsNormalCommands(t *testing.T) {
	cases := []string{"look", "take lamp", "go north", "open mailbox"}
	for _, cmd := range cases {
		if err := Validate(cmd, false); err != nil {
			t.Fatalf("expected %q to be allowed: %v", cmd, err)
		}
	}
}

func TestValidateBlocksEmpty(t *testing.T) {
	if err := Validate("", false); err != ErrNotAllowed {
		t.Fatalf("expected ErrNotAllowed, got %v", err)
	}
	if err := Validate("   ", false); err != ErrNotAllowed {
		t.Fatalf("expected ErrNotAllowed, got %v", err)
	}
}

func TestValidateBlocksDollarCommands(t *testing.T) {
	cases := []string{"$help", "$quit", "$undo", "$dump", "$teleport"}
	for _, cmd := range cases {
		if err := Validate(cmd, false); err != ErrNotAllowed {
			t.Fatalf("expected %q to be blocked: %v", cmd, err)
		}
	}
}

func TestValidateBlocksAnyDollarPrefix(t *testing.T) {
	if err := Validate("$custom", false); err != ErrNotAllowed {
		t.Fatalf("expected ErrNotAllowed, got %v", err)
	}
}

func TestValidateBlocksSaveRestoreForNonAdmin(t *testing.T) {
	for _, cmd := range []string{"save", "restore", "SAVE", "Restore"} {
		if err := Validate(cmd, false); err != ErrNotAllowed {
			t.Fatalf("expected %q to be blocked for non-admin: %v", cmd, err)
		}
	}
}

func TestValidateAllowsSaveRestoreForAdmin(t *testing.T) {
	for _, cmd := range []string{"save", "restore"} {
		if err := Validate(cmd, true); err != nil {
			t.Fatalf("expected %q to be allowed for admin: %v", cmd, err)
		}
	}
}

func TestValidateBlocksLongInput(t *testing.T) {
	long := stringsOfLength(81)
	if err := Validate(long, false); err != ErrNotAllowed {
		t.Fatalf("expected ErrNotAllowed, got %v", err)
	}
}

func TestValidateBlocksNewlines(t *testing.T) {
	if err := Validate("look\nnorth", false); err != ErrNotAllowed {
		t.Fatalf("expected ErrNotAllowed, got %v", err)
	}
}

func TestValidateBlocksControlCharacters(t *testing.T) {
	if err := Validate("look\x00", false); err != ErrNotAllowed {
		t.Fatalf("expected ErrNotAllowed, got %v", err)
	}
	if err := Validate("look\x1b[31m", false); err != ErrNotAllowed {
		t.Fatalf("expected ErrNotAllowed, got %v", err)
	}
}

func stringsOfLength(n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = 'a'
	}
	return string(b)
}
