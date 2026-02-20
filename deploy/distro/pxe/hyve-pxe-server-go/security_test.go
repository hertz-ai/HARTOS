package main

import (
	"os"
	"path/filepath"
	"testing"
)

// ── Path Traversal Prevention Tests ──

func TestPathTraversal_DotDot(t *testing.T) {
	serveDir := t.TempDir()

	attacks := []string{
		"../etc/passwd",
		"../../../etc/shadow",
		"..\\etc\\passwd",
		"..\\..\\..\\windows\\system32\\config\\sam",
	}

	for _, attack := range attacks {
		_, err := ValidatePath(serveDir, attack)
		if err == nil {
			t.Errorf("ValidatePath should reject %q but returned nil error", attack)
		}
	}
}

func TestPathTraversal_Nested(t *testing.T) {
	serveDir := t.TempDir()

	// Create a valid subdirectory so the path prefix looks legitimate.
	os.MkdirAll(filepath.Join(serveDir, "subdir"), 0755)

	attacks := []string{
		"subdir/../../etc/passwd",
		"subdir/../../../etc/shadow",
		"a/b/c/../../../../etc/passwd",
		"./subdir/../../etc/passwd",
	}

	for _, attack := range attacks {
		_, err := ValidatePath(serveDir, attack)
		if err == nil {
			t.Errorf("ValidatePath should reject nested traversal %q but returned nil error", attack)
		}
	}
}

func TestPathTraversal_Encoded(t *testing.T) {
	serveDir := t.TempDir()

	// These are the raw strings a client might send. filepath.Clean handles
	// these because Go's filepath does not interpret percent-encoding — it
	// treats %2e as literal characters, which then fail the Rel check.
	attacks := []string{
		"....//....//etc/passwd",
		"./.../../etc/passwd",
		"....\\....\\etc\\passwd",
	}

	for _, attack := range attacks {
		_, err := ValidatePath(serveDir, attack)
		if err == nil {
			t.Errorf("ValidatePath should reject encoded traversal %q but returned nil error", attack)
		}
	}
}

func TestPathTraversal_NullByte(t *testing.T) {
	serveDir := t.TempDir()

	// Null byte injection: try to truncate path interpretation.
	attacks := []string{
		"vmlinuz\x00../etc/passwd",
		"\x00../etc/passwd",
		"../etc/passwd\x00vmlinuz",
	}

	for _, attack := range attacks {
		result, err := ValidatePath(serveDir, attack)
		if err != nil {
			// Rejected — that is acceptable.
			continue
		}
		// If it didn't error, verify the path is still under serveDir.
		rel, relErr := filepath.Rel(serveDir, result)
		if relErr != nil || len(rel) >= 2 && rel[:2] == ".." {
			t.Errorf("ValidatePath allowed null-byte attack %q to escape: result=%s", attack, result)
		}
	}
}

func TestValidPath_Kernel(t *testing.T) {
	serveDir := t.TempDir()

	// Create the file so the path is actually valid.
	testFile := filepath.Join(serveDir, "vmlinuz")
	os.WriteFile(testFile, []byte("kernel"), 0644)

	result, err := ValidatePath(serveDir, "vmlinuz")
	if err != nil {
		t.Fatalf("valid kernel path rejected: %v", err)
	}

	expected := filepath.Join(serveDir, "vmlinuz")
	if result != expected {
		t.Errorf("result = %s, want %s", result, expected)
	}
}

func TestValidPath_Subdirectory(t *testing.T) {
	serveDir := t.TempDir()

	// Create nested structure.
	pxeDir := filepath.Join(serveDir, "pxelinux.cfg")
	os.MkdirAll(pxeDir, 0755)
	testFile := filepath.Join(pxeDir, "default")
	os.WriteFile(testFile, []byte("config"), 0644)

	casperDir := filepath.Join(serveDir, "casper")
	os.MkdirAll(casperDir, 0755)
	squashfs := filepath.Join(casperDir, "filesystem.squashfs")
	os.WriteFile(squashfs, []byte("squashfs"), 0644)

	tests := []struct {
		name     string
		path     string
		wantFile string
	}{
		{"pxe config", "pxelinux.cfg/default", testFile},
		{"squashfs", "casper/filesystem.squashfs", squashfs},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result, err := ValidatePath(serveDir, tt.path)
			if err != nil {
				t.Fatalf("valid path %q rejected: %v", tt.path, err)
			}
			if result != tt.wantFile {
				t.Errorf("result = %s, want %s", result, tt.wantFile)
			}
		})
	}
}

func TestValidPath_CurrentDir(t *testing.T) {
	serveDir := t.TempDir()

	testFile := filepath.Join(serveDir, "initrd")
	os.WriteFile(testFile, []byte("initrd"), 0644)

	// Dot-prefixed paths should resolve to serve dir.
	result, err := ValidatePath(serveDir, "./initrd")
	if err != nil {
		t.Fatalf("dot-prefixed path rejected: %v", err)
	}
	if result != testFile {
		t.Errorf("result = %s, want %s", result, testFile)
	}
}

func TestValidPath_LeadingSlash(t *testing.T) {
	serveDir := t.TempDir()

	testFile := filepath.Join(serveDir, "vmlinuz")
	os.WriteFile(testFile, []byte("kernel"), 0644)

	// Leading slash in TFTP filename is common — should resolve under serveDir.
	result, err := ValidatePath(serveDir, "/vmlinuz")
	if err != nil {
		// On some OS, /vmlinuz resolves as absolute. That is fine as long as
		// it does not return a path outside serveDir.
		t.Logf("Leading slash path rejected (acceptable): %v", err)
		return
	}
	if result != testFile {
		t.Errorf("result = %s, want %s", result, testFile)
	}
}
