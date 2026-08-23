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

func hasFilenamePrompt(buf []byte) bool {
	cleaned := strings.TrimRight(stripANSI(string(buf)), " \t\n\r")
	return strings.Contains(cleaned, "Filename") && strings.HasSuffix(cleaned, ":")
}

func isSaveRestoreCommand(command string) bool {
	switch strings.ToLower(strings.TrimSpace(command)) {
	case "save", "restore":
		return true
	default:
		return false
	}
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

	start := skipCommandEcho(lines, command)
	if start == 0 {
		start = skipCommandEchoLine(lines, command)
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

func skipCommandEchoLine(lines []string, command string) int {
	command = strings.TrimSpace(command)
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if !strings.HasPrefix(trimmed, ">") {
			continue
		}
		echo := strings.TrimSpace(strings.TrimPrefix(trimmed, ">"))
		if strings.EqualFold(echo, command) {
			return i + 1
		}
	}
	return 0
}

func skipCommandEcho(lines []string, command string) int {
	command = strings.TrimSpace(command)
	if command == "" {
		return 0
	}

	normalizedCommand := normalizeEcho(command)
	echo := ""
	start := -1

	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if echo == "" {
			if !strings.HasPrefix(trimmed, ">") {
				return 0
			}
			start = i
			echo = strings.TrimSpace(strings.TrimPrefix(trimmed, ">"))
		} else if trimmed != "" {
			echo += trimmed
		}

		normalizedEcho := normalizeEcho(echo)
		if normalizedEcho == normalizedCommand {
			return i + 1
		}
		if !strings.HasPrefix(normalizedCommand, normalizedEcho) {
			return 0
		}
	}

	if start >= 0 && normalizeEcho(echo) == normalizedCommand {
		return len(lines)
	}
	return 0
}

func normalizeEcho(s string) string {
	return strings.ToLower(strings.Join(strings.Fields(s), ""))
}
