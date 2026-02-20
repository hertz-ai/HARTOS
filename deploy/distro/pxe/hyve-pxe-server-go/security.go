package main

import (
	"fmt"
	"path/filepath"
	"strings"
)

// ValidatePath ensures the requested path stays under serveDir.
//
// This is the primary defense against path traversal attacks in the TFTP server.
// Uses filepath.Rel which is immune to encoding tricks (percent-encoded slashes,
// null bytes, backslash normalization, etc.) because it operates on cleaned paths.
//
// Returns the absolute path to the file if valid, or an error if traversal detected.
func ValidatePath(serveDir, requested string) (string, error) {
	// Strip null bytes — TFTP filenames should never contain them.
	requested = strings.ReplaceAll(requested, "\x00", "")

	// Clean the requested path (resolves . and .., normalizes separators).
	clean := filepath.Clean(requested)

	// Join with serve directory and clean again.
	abs := filepath.Join(serveDir, clean)
	abs = filepath.Clean(abs)

	// Compute relative path from serveDir to the target.
	// If the target is outside serveDir, rel will start with "..".
	rel, err := filepath.Rel(serveDir, abs)
	if err != nil {
		return "", fmt.Errorf("path validation failed: %w", err)
	}

	// Reject if the relative path escapes the serve directory.
	if strings.HasPrefix(rel, "..") || strings.HasPrefix(rel, string(filepath.Separator)+"..") {
		return "", fmt.Errorf("path traversal detected: %s", requested)
	}

	// Reject absolute paths that don't match the prefix (belt and suspenders).
	if !strings.HasPrefix(abs, serveDir) {
		return "", fmt.Errorf("path traversal detected: %s", requested)
	}

	return abs, nil
}
