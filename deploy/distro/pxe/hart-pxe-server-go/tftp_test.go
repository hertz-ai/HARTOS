package main

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
)

// ── ValidatePath tests (via security.go, exercised from TFTP context) ──

func TestValidatePath_Normal(t *testing.T) {
	serveDir := t.TempDir()

	// Create a test file.
	testFile := filepath.Join(serveDir, "vmlinuz")
	if err := os.WriteFile(testFile, []byte("kernel"), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := ValidatePath(serveDir, "vmlinuz")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result != testFile {
		t.Errorf("expected %s, got %s", testFile, result)
	}
}

func TestValidatePath_Traversal(t *testing.T) {
	serveDir := t.TempDir()

	traversalPaths := []string{
		"../etc/passwd",
		"../../etc/shadow",
		"....//....//etc/passwd",
		"subdir/../../etc/passwd",
		"./../../etc/passwd",
	}

	for _, p := range traversalPaths {
		_, err := ValidatePath(serveDir, p)
		if err == nil {
			t.Errorf("expected error for traversal path %q, got nil", p)
		}
	}
}

func TestValidatePath_AbsolutePath(t *testing.T) {
	serveDir := t.TempDir()

	// Absolute paths outside serve dir should be blocked.
	_, err := ValidatePath(serveDir, "/etc/passwd")
	if err == nil {
		t.Error("expected error for absolute path /etc/passwd, got nil")
	}
}

func TestValidatePath_Subdirectory(t *testing.T) {
	serveDir := t.TempDir()

	// Create subdirectory with file.
	subDir := filepath.Join(serveDir, "pxelinux.cfg")
	if err := os.MkdirAll(subDir, 0755); err != nil {
		t.Fatal(err)
	}
	testFile := filepath.Join(subDir, "default")
	if err := os.WriteFile(testFile, []byte("config"), 0644); err != nil {
		t.Fatal(err)
	}

	result, err := ValidatePath(serveDir, "pxelinux.cfg/default")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result != testFile {
		t.Errorf("expected %s, got %s", testFile, result)
	}
}

// ── RRQ Parsing tests ──

func TestRRQParsing(t *testing.T) {
	tests := []struct {
		name         string
		data         []byte
		wantFilename string
		wantMode     string
	}{
		{
			name:         "normal octet request",
			data:         append(append([]byte("vmlinuz"), 0), append([]byte("octet"), 0)...),
			wantFilename: "vmlinuz",
			wantMode:     "octet",
		},
		{
			name:         "netascii mode",
			data:         append(append([]byte("pxelinux.cfg/default"), 0), append([]byte("netascii"), 0)...),
			wantFilename: "pxelinux.cfg/default",
			wantMode:     "netascii",
		},
		{
			name:         "empty data",
			data:         []byte{},
			wantFilename: "",
			wantMode:     "",
		},
		{
			name:         "no null terminator",
			data:         []byte("vmlinuz"),
			wantFilename: "",
			wantMode:     "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			filename, mode := parseRRQ(tt.data)
			if filename != tt.wantFilename {
				t.Errorf("filename = %q, want %q", filename, tt.wantFilename)
			}
			if mode != tt.wantMode {
				t.Errorf("mode = %q, want %q", mode, tt.wantMode)
			}
		})
	}
}

// ── Packet format tests ──

func TestErrorPacketFormat(t *testing.T) {
	pkt := makeErrorPacket(1, "File not found")

	if len(pkt) < 5 {
		t.Fatalf("packet too short: %d bytes", len(pkt))
	}

	opcode := binary.BigEndian.Uint16(pkt[0:2])
	if opcode != opERROR {
		t.Errorf("opcode = %d, want %d (ERROR)", opcode, opERROR)
	}

	errCode := binary.BigEndian.Uint16(pkt[2:4])
	if errCode != 1 {
		t.Errorf("error code = %d, want 1", errCode)
	}

	// Message should be null-terminated.
	msg := string(pkt[4 : len(pkt)-1])
	if msg != "File not found" {
		t.Errorf("message = %q, want %q", msg, "File not found")
	}

	if pkt[len(pkt)-1] != 0 {
		t.Error("error packet not null-terminated")
	}
}

func TestDataPacketFormat(t *testing.T) {
	data := []byte("Hello, TFTP!")
	pkt := makeDataPacket(42, data)

	if len(pkt) != 4+len(data) {
		t.Fatalf("packet length = %d, want %d", len(pkt), 4+len(data))
	}

	opcode := binary.BigEndian.Uint16(pkt[0:2])
	if opcode != opDATA {
		t.Errorf("opcode = %d, want %d (DATA)", opcode, opDATA)
	}

	blockNum := binary.BigEndian.Uint16(pkt[2:4])
	if blockNum != 42 {
		t.Errorf("block number = %d, want 42", blockNum)
	}

	payload := pkt[4:]
	if string(payload) != "Hello, TFTP!" {
		t.Errorf("payload = %q, want %q", string(payload), "Hello, TFTP!")
	}
}

func TestBlockSize(t *testing.T) {
	if blockSize != 512 {
		t.Errorf("blockSize = %d, want 512 (RFC 1350)", blockSize)
	}
}

func TestDataPacketMaxBlock(t *testing.T) {
	// Block number should handle uint16 max.
	data := make([]byte, blockSize)
	pkt := makeDataPacket(65535, data)

	blockNum := binary.BigEndian.Uint16(pkt[2:4])
	if blockNum != 65535 {
		t.Errorf("block number = %d, want 65535", blockNum)
	}
}

func TestErrorPacketEmptyMessage(t *testing.T) {
	pkt := makeErrorPacket(0, "")

	opcode := binary.BigEndian.Uint16(pkt[0:2])
	if opcode != opERROR {
		t.Errorf("opcode = %d, want %d", opcode, opERROR)
	}

	errCode := binary.BigEndian.Uint16(pkt[2:4])
	if errCode != 0 {
		t.Errorf("error code = %d, want 0", errCode)
	}

	// Should have just the null terminator after the header.
	if pkt[4] != 0 {
		t.Error("expected null terminator at position 4 for empty message")
	}
}
