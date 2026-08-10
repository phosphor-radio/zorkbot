package sanitize

import (
	"errors"
	"strings"
	"unicode"
)

var ErrNotAllowed = errors.New("that command isn't allowed")

var blockedDebugCommands = map[string]struct{}{
	"$help": {}, "$quit": {}, "$undo": {}, "$redo": {}, "$dump": {},
	"$dict": {}, "$tree": {}, "$room": {}, "$you": {}, "$find": {},
	"$object": {}, "$parent": {}, "$attrs": {}, "$props": {}, "$simple": {},
	"$header": {}, "$history": {}, "$have_attr": {}, "$have_prop": {},
	"$steal": {}, "$teleport": {},
}

const maxCommandLength = 80

// Validate checks whether text may be forwarded to encrusted.
func Validate(text string, admin bool) error {
	text = strings.TrimSpace(text)
	if text == "" {
		return ErrNotAllowed
	}
	if len(text) > maxCommandLength {
		return ErrNotAllowed
	}
	if strings.ContainsAny(text, "\n\r") {
		return ErrNotAllowed
	}
	if strings.HasPrefix(text, "$") {
		return ErrNotAllowed
	}
	if _, blocked := blockedDebugCommands[strings.ToLower(text)]; blocked {
		return ErrNotAllowed
	}
	if hasControlOrANSI(text) {
		return ErrNotAllowed
	}
	lower := strings.ToLower(text)
	if !admin && (lower == "save" || lower == "restore" || lower == "quit") {
		return ErrNotAllowed
	}
	return nil
}

func hasControlOrANSI(text string) bool {
	for _, r := range text {
		if r == unicode.ReplacementChar {
			continue
		}
		if r < 0x20 || r == 0x7f {
			return true
		}
		if r == '\x1b' {
			return true
		}
	}
	return false
}
