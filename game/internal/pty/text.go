package pty

import (
	"regexp"
	"strings"
)

var (
	ansiEscape = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]`)
	oscEscape  = regexp.MustCompile(`\x1b\][^\x07]*(?:\x07|\x1b\\)`)
)

func stripANSI(s string) string {
	s = oscEscape.ReplaceAllString(s, "")
	s = ansiEscape.ReplaceAllString(s, "")
	return s
}

func normalizeNewlines(s string) string {
	s = strings.ReplaceAll(s, "\r\n", "\n")
	s = strings.ReplaceAll(s, "\r", "\n")
	return s
}

func collapseBlankLines(s string) string {
	lines := strings.Split(s, "\n")
	out := make([]string, 0, len(lines))
	blank := false
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			if !blank && len(out) > 0 {
				out = append(out, "")
				blank = true
			}
			continue
		}
		blank = false
		out = append(out, trimmed)
	}
	return strings.TrimSpace(strings.Join(out, "\n"))
}

func hasPrompt(buf []byte) bool {
	cleaned := strings.TrimRight(stripANSI(string(buf)), " \t\n\r")
	return strings.HasSuffix(cleaned, ">")
}

func isPromptOnlyLine(line string) bool {
	trimmed := strings.TrimSpace(stripANSI(line))
	if trimmed == ">" {
		return true
	}
	return strings.HasPrefix(trimmed, ">") && strings.Contains(trimmed, " - ")
}

func extractResponse(raw []byte, command string) string {
	text := normalizeNewlines(stripANSI(string(raw)))
	lines := strings.Split(text, "\n")

	start := 0
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if !strings.HasPrefix(trimmed, ">") {
			continue
		}
		echo := strings.TrimSpace(strings.TrimPrefix(trimmed, ">"))
		if strings.EqualFold(echo, command) {
			start = i + 1
			break
		}
	}

	end := len(lines)
	for i := len(lines) - 1; i >= start; i-- {
		if isPromptOnlyLine(lines[i]) {
			end = i
			break
		}
	}

	if start >= end {
		return collapseBlankLines(strings.TrimSpace(text))
	}

	return collapseBlankLines(strings.Join(lines[start:end], "\n"))
}
