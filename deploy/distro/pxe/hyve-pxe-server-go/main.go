// HyveOS PXE Boot Server — Go rewrite for high-concurrency TFTP + HTTP.
//
// Replaces the Python hyve-pxe-server.py with a statically compiled binary
// capable of handling 1000s of concurrent TFTP clients via goroutines.
//
// Usage:
//
//	sudo ./hyve-pxe-server --iso /path/to/hyve-os.iso [--http-port 8888] [--tftp-port 69]
//
// Requires:
//   - An existing DHCP server configured with:
//     option 66 (next-server): this machine's IP
//     option 67 (filename): pxelinux.0
//   - OR: dnsmasq in proxy mode (instructions printed on start)
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

func main() {
	serveDir := flag.String("serve-dir", "/srv/hyve-pxe", "Directory to serve PXE files from")
	httpPort := flag.Int("http-port", 8888, "HTTP port for autoinstall + squashfs")
	tftpPort := flag.Int("tftp-port", 69, "TFTP port for PXE boot files")
	isoPath := flag.String("iso", "", "Path to HyveOS ISO file (optional; extracts boot files)")
	iface := flag.String("interface", "", "Network interface to bind to (e.g., eth0)")
	flag.Parse()

	// Ensure serve directory exists.
	if err := os.MkdirAll(*serveDir, 0755); err != nil {
		log.Fatalf("Failed to create serve directory %s: %v", *serveDir, err)
	}

	// Resolve to absolute path for safety.
	absServeDir, err := filepath.Abs(*serveDir)
	if err != nil {
		log.Fatalf("Failed to resolve serve directory: %v", err)
	}

	// Determine server IP.
	serverIP := getServerIP(*iface)
	log.Printf("Server IP: %s", serverIP)

	// Extract ISO if provided.
	if *isoPath != "" {
		if _, err := os.Stat(*isoPath); os.IsNotExist(err) {
			log.Fatalf("ISO not found: %s", *isoPath)
		}
		if err := extractISO(*isoPath, absServeDir); err != nil {
			log.Fatalf("ISO extraction failed: %v", err)
		}
	}

	// Setup PXE config with actual server IP.
	setupPXEConfig(absServeDir, serverIP, *httpPort)

	// Setup autoinstall configs.
	setupAutoinstallDir(absServeDir)

	// Print DHCP configuration hint.
	printDNSMasqHint(serverIP, absServeDir)

	// Verify pxelinux.0 exists.
	pxeBootloader := filepath.Join(absServeDir, "pxelinux.0")
	if _, err := os.Stat(pxeBootloader); os.IsNotExist(err) {
		log.Printf("WARNING: pxelinux.0 not found! Install: apt install pxelinux syslinux-common")
	}

	// Startup info.
	log.Printf("Starting HyveOS PXE server...")
	log.Printf("  TFTP: 0.0.0.0:%d", *tftpPort)
	log.Printf("  HTTP: http://0.0.0.0:%d", *httpPort)
	log.Printf("  Autoinstall: http://%s:%d/autoinstall/", serverIP, *httpPort)
	log.Printf("  Serve dir: %s", absServeDir)

	// Context for graceful shutdown.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start TFTP server in goroutine.
	tftpServer := NewTFTPServer(absServeDir, *tftpPort)
	go func() {
		if err := tftpServer.ListenAndServe(ctx); err != nil {
			log.Printf("TFTP server error: %v", err)
		}
	}()

	// Start HTTP server in goroutine.
	httpServer := NewHTTPServer(absServeDir, *httpPort)
	go func() {
		if err := httpServer.ListenAndServe(); err != nil {
			log.Printf("HTTP server error: %v", err)
		}
	}()

	// Wait for signal (SIGINT/SIGTERM) for graceful shutdown.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	sig := <-sigCh
	log.Printf("Received signal %v, shutting down...", sig)

	cancel() // Stop TFTP server.

	// Use a fresh context for HTTP graceful shutdown (the original is now cancelled).
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		log.Printf("HTTP server shutdown error: %v", err)
	}

	log.Printf("HyveOS PXE server stopped.")
}

// printDNSMasqHint prints dnsmasq proxy DHCP configuration instructions.
func printDNSMasqHint(serverIP, serveDir string) {
	fmt.Printf(`
  +-----------------------------------------------------------+
  | DHCP Configuration Required                               |
  |                                                           |
  | Option A: Configure existing DHCP server:                 |
  |   next-server: %-24s                   |
  |   filename: pxelinux.0                                    |
  |                                                           |
  | Option B: Use dnsmasq proxy mode:                         |
  |   apt install dnsmasq                                     |
  |   # /etc/dnsmasq.d/hyve-pxe.conf:                        |
  |   port=0                                                  |
  |   dhcp-range=<subnet>,proxy                               |
  |   pxe-service=x86PC,"HyveOS",pxelinux                    |
  |   enable-tftp                                             |
  |   tftp-root=%-24s                      |
  +-----------------------------------------------------------+
`, serverIP, truncate(serveDir, 24))
}

func truncate(s string, maxLen int) string {
	if len(s) > maxLen {
		return s[:maxLen]
	}
	return s
}
