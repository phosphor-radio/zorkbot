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
