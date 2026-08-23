package pty

import "testing"

func TestStripANSIAndExtractResponse(t *testing.T) {
	raw := []byte("> look\r\n\x1b[37;1mWest of House\x1b[0m\r\nYou are standing in an open field.\r\n\r\n>\x1b]2;West of House  -  0/1\x07 ")
	got := extractResponse(raw, "look")
	want := "West of House\nYou are standing in an open field."
	if got != want {
		t.Fatalf("extractResponse() = %q, want %q", got, want)
	}
}

func TestHasPrompt(t *testing.T) {
	if !hasPrompt([]byte("West of House\r\n>\x1b]2;Room\x07 ")) {
		t.Fatal("expected prompt at end of buffer")
	}
	if hasPrompt([]byte("West of House\r\n")) {
		t.Fatal("expected no prompt without > suffix")
	}
}

func TestHasFilenamePrompt(t *testing.T) {
	if !hasFilenamePrompt([]byte("save\r\n\r\nFilename [zork1.sav]: ")) {
		t.Fatal("expected filename prompt")
	}
	if hasFilenamePrompt([]byte("West of House\r\n> ")) {
		t.Fatal("expected no filename prompt for game prompt")
	}
}

func TestExtractSaveResponse(t *testing.T) {
	raw := []byte("\r\nOk.\r\n\r\n> ")
	got := extractResponse(raw, "save")
	if got != "Ok." {
		t.Fatalf("extractResponse() = %q, want %q", got, "Ok.")
	}
}

func TestExtractResponseStripsSplitCommandEcho(t *testing.T) {
	raw := []byte("> ta\r\nke peppers\r\nYou can't see any peppers here!\r\n\r\n> ")
	got := extractResponse(raw, "take peppers")
	want := "You can't see any peppers here!"
	if got != want {
		t.Fatalf("extractResponse() = %q, want %q", got, want)
	}
}

func TestExtractResponseStripsSplitCommandEchoTakeSack(t *testing.T) {
	raw := []byte("> ta\r\nke sack\r\nTaken.\r\n\r\n> ")
	got := extractResponse(raw, "take sack")
	want := "Taken."
	if got != want {
		t.Fatalf("extractResponse() = %q, want %q", got, want)
	}
}

// Echo arrives without ">" prefix — the common case when the ">" prompt was
// consumed at the end of the previous command's read buffer.
func TestExtractResponseStripsEchoNoPrefixFullRoom(t *testing.T) {
	raw := []byte("enter house\r\nKitchen\r\nYou are in the kitchen of the white house.\r\n\r\n> ")
	got := extractResponse(raw, "enter house")
	want := "Kitchen\nYou are in the kitchen of the white house."
	if got != want {
		t.Fatalf("extractResponse() = %q, want %q", got, want)
	}
}

func TestExtractResponseStripsEchoNoPrefixShort(t *testing.T) {
	raw := []byte("open window\r\nWith great effort, you open the window far enough to allow entry.\r\n> ")
	got := extractResponse(raw, "open window")
	want := "With great effort, you open the window far enough to allow entry."
	if got != want {
		t.Fatalf("extractResponse() = %q, want %q", got, want)
	}
}

func TestExtractResponseStripsEchoNoPrefixWithLeadingBlank(t *testing.T) {
	raw := []byte("\r\nenter house\r\nKitchen\r\nYou are in the kitchen.\r\n\r\n> ")
	got := extractResponse(raw, "enter house")
	want := "Kitchen\nYou are in the kitchen."
	if got != want {
		t.Fatalf("extractResponse() = %q, want %q", got, want)
	}
}

func TestExtractResponseStripsEchoNoPrefixSplit(t *testing.T) {
	raw := []byte("ta\r\nke peppers\r\nYou can't see any peppers here!\r\n\r\n> ")
	got := extractResponse(raw, "take peppers")
	want := "You can't see any peppers here!"
	if got != want {
		t.Fatalf("extractResponse() = %q, want %q", got, want)
	}
}
