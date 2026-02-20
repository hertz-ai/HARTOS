package main

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"time"
)

// TFTP opcodes per RFC 1350.
const (
	opRRQ   uint16 = 1 // Read request
	opWRQ   uint16 = 2 // Write request (not supported)
	opDATA  uint16 = 3 // Data
	opACK   uint16 = 4 // Acknowledgement
	opERROR uint16 = 5 // Error
)

// TFTP error codes.
const (
	errNotDefined    uint16 = 0
	errFileNotFound  uint16 = 1
	errAccessDenied  uint16 = 2
	errIllegalOp     uint16 = 4
)

const (
	blockSize  = 512
	ackTimeout = 5 * time.Second
	maxRetries = 3
)

// TFTPServer handles concurrent TFTP read requests.
type TFTPServer struct {
	serveDir string
	port     int
}

// NewTFTPServer creates a new TFTP server.
func NewTFTPServer(serveDir string, port int) *TFTPServer {
	return &TFTPServer{
		serveDir: serveDir,
		port:     port,
	}
}

// ListenAndServe starts the TFTP server. It blocks until the context is cancelled.
func (s *TFTPServer) ListenAndServe(ctx context.Context) error {
	addr, err := net.ResolveUDPAddr("udp", fmt.Sprintf(":%d", s.port))
	if err != nil {
		return fmt.Errorf("resolve address: %w", err)
	}

	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		return fmt.Errorf("listen UDP :%d: %w", s.port, err)
	}
	defer conn.Close()

	log.Printf("[TFTP] Listening on :%d (dir: %s)", s.port, s.serveDir)

	// Shutdown goroutine: close conn when context is cancelled.
	go func() {
		<-ctx.Done()
		conn.Close()
	}()

	buf := make([]byte, 516) // max TFTP packet = 4 header + 512 data
	for {
		n, clientAddr, err := conn.ReadFromUDP(buf)
		if err != nil {
			// Check if we were shut down.
			select {
			case <-ctx.Done():
				return nil
			default:
			}
			log.Printf("[TFTP] Read error: %v", err)
			continue
		}

		if n < 4 {
			continue
		}

		opcode := binary.BigEndian.Uint16(buf[:2])

		switch opcode {
		case opRRQ:
			filename, _ := parseRRQ(buf[2:n])
			if filename == "" {
				continue
			}
			// Each client gets its own goroutine and transfer socket.
			go s.handleRRQ(clientAddr, filename)
		case opWRQ:
			go s.sendErrorToAddr(clientAddr, errIllegalOp, "Write not supported — read-only server")
		default:
			go s.sendErrorToAddr(clientAddr, errIllegalOp, "Illegal operation")
		}
	}
}

// parseRRQ extracts the filename and mode from an RRQ packet body.
// RRQ format: filename\0mode\0
func parseRRQ(data []byte) (filename string, mode string) {
	// Find first null byte (end of filename).
	var fnEnd int
	for fnEnd = 0; fnEnd < len(data); fnEnd++ {
		if data[fnEnd] == 0 {
			break
		}
	}
	if fnEnd >= len(data) {
		return "", ""
	}
	filename = string(data[:fnEnd])

	// Find mode string.
	modeStart := fnEnd + 1
	var modeEnd int
	for modeEnd = modeStart; modeEnd < len(data); modeEnd++ {
		if data[modeEnd] == 0 {
			break
		}
	}
	if modeEnd > modeStart {
		mode = string(data[modeStart:modeEnd])
	}

	return filename, mode
}

// handleRRQ serves a file to the client via a new per-transfer UDP socket.
func (s *TFTPServer) handleRRQ(clientAddr *net.UDPAddr, filename string) {
	// Validate path (security: prevent traversal).
	absPath, err := ValidatePath(s.serveDir, filename)
	if err != nil {
		log.Printf("[TFTP] Path traversal blocked: %s (%v)", filename, err)
		s.sendErrorToAddr(clientAddr, errAccessDenied, "Access denied")
		return
	}

	// Check file exists and is a regular file.
	info, err := os.Stat(absPath)
	if os.IsNotExist(err) {
		log.Printf("[TFTP] File not found: %s", filename)
		s.sendErrorToAddr(clientAddr, errFileNotFound, "File not found")
		return
	}
	if err != nil {
		log.Printf("[TFTP] Stat error for %s: %v", filename, err)
		s.sendErrorToAddr(clientAddr, errNotDefined, "Internal error")
		return
	}
	if info.IsDir() {
		log.Printf("[TFTP] Requested path is a directory: %s", filename)
		s.sendErrorToAddr(clientAddr, errFileNotFound, "File not found")
		return
	}

	log.Printf("[TFTP] Serving: %s (%d bytes) -> %s", filename, info.Size(), clientAddr)

	// Open a new UDP socket for this transfer (RFC 1350: TID).
	transferConn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		log.Printf("[TFTP] Failed to create transfer socket: %v", err)
		return
	}
	defer transferConn.Close()

	// Open the file.
	f, err := os.Open(absPath)
	if err != nil {
		log.Printf("[TFTP] Error opening %s: %v", absPath, err)
		sendError(transferConn, clientAddr, errNotDefined, err.Error())
		return
	}
	defer f.Close()

	// Send file in 512-byte blocks.
	buf := make([]byte, blockSize)
	ackBuf := make([]byte, 4)
	var blockNum uint16 = 1

	for {
		n, readErr := f.Read(buf)
		if readErr != nil && readErr != io.EOF {
			log.Printf("[TFTP] Read error on %s: %v", filename, readErr)
			sendError(transferConn, clientAddr, errNotDefined, readErr.Error())
			return
		}

		// Build DATA packet: opcode(2) + block#(2) + data(n).
		dataPacket := makeDataPacket(blockNum, buf[:n])

		// Send with retry.
		acked := false
		for retry := 0; retry < maxRetries; retry++ {
			_, err := transferConn.WriteToUDP(dataPacket, clientAddr)
			if err != nil {
				log.Printf("[TFTP] Send error block %d: %v", blockNum, err)
				return
			}

			// Wait for ACK.
			transferConn.SetReadDeadline(time.Now().Add(ackTimeout))
			ackN, _, ackErr := transferConn.ReadFromUDP(ackBuf)
			if ackErr != nil {
				log.Printf("[TFTP] Timeout waiting for ACK block %d (retry %d/%d)",
					blockNum, retry+1, maxRetries)
				continue
			}

			if ackN < 4 {
				continue
			}

			ackOpcode := binary.BigEndian.Uint16(ackBuf[:2])
			ackBlock := binary.BigEndian.Uint16(ackBuf[2:4])

			if ackOpcode == opACK && ackBlock == blockNum {
				acked = true
				break
			}
		}

		if !acked {
			log.Printf("[TFTP] Transfer aborted for %s at block %d (no ACK after %d retries)",
				filename, blockNum, maxRetries)
			return
		}

		// If this was the last block (< 512 bytes), we are done.
		if n < blockSize {
			log.Printf("[TFTP] Transfer complete: %s (%d blocks)", filename, blockNum)
			return
		}

		blockNum++
	}
}

// makeDataPacket builds a TFTP DATA packet.
func makeDataPacket(blockNum uint16, data []byte) []byte {
	pkt := make([]byte, 4+len(data))
	binary.BigEndian.PutUint16(pkt[0:2], opDATA)
	binary.BigEndian.PutUint16(pkt[2:4], blockNum)
	copy(pkt[4:], data)
	return pkt
}

// makeErrorPacket builds a TFTP ERROR packet.
func makeErrorPacket(code uint16, msg string) []byte {
	msgBytes := []byte(msg)
	pkt := make([]byte, 5+len(msgBytes))
	binary.BigEndian.PutUint16(pkt[0:2], opERROR)
	binary.BigEndian.PutUint16(pkt[2:4], code)
	copy(pkt[4:], msgBytes)
	pkt[4+len(msgBytes)] = 0 // null terminator
	return pkt
}

// sendError sends a TFTP error packet on a given connection.
func sendError(conn *net.UDPConn, addr *net.UDPAddr, code uint16, msg string) {
	pkt := makeErrorPacket(code, msg)
	conn.WriteToUDP(pkt, addr)
}

// sendErrorToAddr sends a TFTP error packet using a temporary connection.
// Used when we don't have a transfer socket yet.
func (s *TFTPServer) sendErrorToAddr(addr *net.UDPAddr, code uint16, msg string) {
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		log.Printf("[TFTP] Failed to create socket for error response: %v", err)
		return
	}
	defer conn.Close()
	sendError(conn, addr, code, msg)
}
