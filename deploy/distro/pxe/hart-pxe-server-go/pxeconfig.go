package main

import (
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"runtime"
)

// getServerIP returns the IP address of the specified interface, or the first
// non-loopback address if no interface is specified.
func getServerIP(iface string) string {
	// If a specific interface is requested, try to get its IP.
	if iface != "" {
		netIface, err := net.InterfaceByName(iface)
		if err == nil {
			addrs, err := netIface.Addrs()
			if err == nil {
				for _, addr := range addrs {
					if ipNet, ok := addr.(*net.IPNet); ok && ipNet.IP.To4() != nil {
						return ipNet.IP.String()
					}
				}
			}
		}
		log.Printf("Warning: could not get IP for interface %s, falling back to auto-detect", iface)
	}

	// Fallback: connect to external address and read local endpoint.
	// This picks the default route interface without actually sending traffic.
	conn, err := net.Dial("udp", "8.8.8.8:80")
	if err == nil {
		defer conn.Close()
		if localAddr, ok := conn.LocalAddr().(*net.UDPAddr); ok {
			return localAddr.IP.String()
		}
	}

	// Last resort: scan all interfaces.
	ifaces, err := net.Interfaces()
	if err == nil {
		for _, i := range ifaces {
			if i.Flags&net.FlagLoopback != 0 {
				continue
			}
			if i.Flags&net.FlagUp == 0 {
				continue
			}
			addrs, err := i.Addrs()
			if err != nil {
				continue
			}
			for _, addr := range addrs {
				if ipNet, ok := addr.(*net.IPNet); ok && ipNet.IP.To4() != nil {
					return ipNet.IP.String()
				}
			}
		}
	}

	return "0.0.0.0"
}

// setupPXEConfig writes the pxelinux.cfg/default file with the correct server IP.
func setupPXEConfig(serveDir, serverIP string, httpPort int) {
	pxeCfgDir := filepath.Join(serveDir, "pxelinux.cfg")
	if err := os.MkdirAll(pxeCfgDir, 0755); err != nil {
		log.Printf("Warning: failed to create pxelinux.cfg dir: %v", err)
		return
	}

	config := fmt.Sprintf(`DEFAULT hart-auto
PROMPT 1
TIMEOUT 100
MENU TITLE HART OS Network Install

LABEL hart-auto
    MENU LABEL ^HART OS — Automatic Install
    MENU DEFAULT
    KERNEL vmlinuz
    APPEND initrd=initrd ip=dhcp autoinstall ds=nocloud-net;s=http://%s:%d/autoinstall/ ---

LABEL hart-manual
    MENU LABEL HART OS — ^Manual Install
    KERNEL vmlinuz
    APPEND initrd=initrd ip=dhcp ---

LABEL local
    MENU LABEL Boot from ^local disk
    LOCALBOOT 0
`, serverIP, httpPort)

	cfgPath := filepath.Join(pxeCfgDir, "default")
	if err := os.WriteFile(cfgPath, []byte(config), 0644); err != nil {
		log.Printf("Warning: failed to write PXE config: %v", err)
		return
	}

	log.Printf("PXE config written with server IP: %s", serverIP)
}

// setupAutoinstallDir copies autoinstall configs (user-data, meta-data, vendor-data)
// from the repo's autoinstall directory to the serve directory.
func setupAutoinstallDir(serveDir string) {
	autoinstallDir := filepath.Join(serveDir, "autoinstall")
	if err := os.MkdirAll(autoinstallDir, 0755); err != nil {
		log.Printf("Warning: failed to create autoinstall dir: %v", err)
		return
	}

	// Determine the repo autoinstall directory relative to this binary's location.
	// In the repo layout: deploy/distro/pxe/hart-pxe-server-go/ -> deploy/distro/autoinstall/
	repoAutoinstall := findRepoAutoinstallDir()
	if repoAutoinstall == "" {
		log.Printf("Warning: repo autoinstall directory not found; skipping config copy")
		return
	}

	filesToCopy := []string{"user-data", "meta-data", "vendor-data"}
	for _, filename := range filesToCopy {
		src := filepath.Join(repoAutoinstall, filename)
		dst := filepath.Join(autoinstallDir, filename)
		if err := copyFileIfExists(src, dst); err != nil {
			log.Printf("Warning: failed to copy %s: %v", filename, err)
		}
	}
}

// findRepoAutoinstallDir tries to locate the autoinstall directory in the repo tree.
func findRepoAutoinstallDir() string {
	// Try relative to the binary's directory.
	execPath, err := os.Executable()
	if err == nil {
		execDir := filepath.Dir(execPath)
		// deploy/distro/pxe/hart-pxe-server-go/ -> deploy/distro/autoinstall/
		candidate := filepath.Join(execDir, "..", "..", "autoinstall")
		if dirExists(candidate) {
			return candidate
		}
	}

	// Try relative to current working directory.
	cwd, err := os.Getwd()
	if err == nil {
		// If running from the pxe directory.
		candidate := filepath.Join(cwd, "..", "autoinstall")
		if dirExists(candidate) {
			return candidate
		}
		// If running from the repo root.
		candidate = filepath.Join(cwd, "deploy", "distro", "autoinstall")
		if dirExists(candidate) {
			return candidate
		}
	}

	// Try GOPATH-based resolution for development.
	if runtime.GOOS == "linux" {
		candidate := "/opt/hart/deploy/distro/autoinstall"
		if dirExists(candidate) {
			return candidate
		}
	}

	return ""
}

// dirExists returns true if path exists and is a directory.
func dirExists(path string) bool {
	info, err := os.Stat(path)
	if err != nil {
		return false
	}
	return info.IsDir()
}
