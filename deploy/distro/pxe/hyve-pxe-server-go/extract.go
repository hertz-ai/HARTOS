package main

import (
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
)

// extractISO mounts the ISO and copies boot files to the output directory.
//
// Extracted files:
//   - vmlinuz        (kernel)
//   - initrd         (initramfs)
//   - casper/filesystem.squashfs
//   - pxelinux.0     (PXE bootloader)
//   - ldlinux.c32    (syslinux dependency)
//   - libutil.c32    (syslinux dependency)
//   - menu.c32       (syslinux menu)
func extractISO(isoPath, outputDir string) error {
	log.Printf("Extracting ISO: %s -> %s", isoPath, outputDir)

	// Create a temporary mount point.
	mountPoint, err := os.MkdirTemp("", "hyve-iso-")
	if err != nil {
		return fmt.Errorf("create temp mount point: %w", err)
	}
	defer os.RemoveAll(mountPoint)

	// Mount the ISO read-only.
	mountCmd := exec.Command("mount", "-o", "loop,ro", isoPath, mountPoint)
	if output, err := mountCmd.CombinedOutput(); err != nil {
		return fmt.Errorf("mount ISO: %w\n%s", err, string(output))
	}
	defer func() {
		umountCmd := exec.Command("umount", mountPoint)
		if err := umountCmd.Run(); err != nil {
			log.Printf("Warning: failed to unmount %s: %v", mountPoint, err)
		}
	}()

	// Copy kernel and initrd.
	bootFiles := map[string]string{
		"casper/vmlinuz": "vmlinuz",
		"casper/initrd":  "initrd",
	}
	for src, dst := range bootFiles {
		srcPath := filepath.Join(mountPoint, src)
		dstPath := filepath.Join(outputDir, dst)
		if err := copyFileIfExists(srcPath, dstPath); err != nil {
			log.Printf("Warning: failed to copy %s: %v", src, err)
		} else if fileExists(srcPath) {
			log.Printf("Extracted: %s", dst)
		}
	}

	// Copy squashfs for HTTP serving.
	squashfsSrc := filepath.Join(mountPoint, "casper/filesystem.squashfs")
	if fileExists(squashfsSrc) {
		casperDir := filepath.Join(outputDir, "casper")
		if err := os.MkdirAll(casperDir, 0755); err != nil {
			return fmt.Errorf("create casper dir: %w", err)
		}
		squashfsDst := filepath.Join(casperDir, "filesystem.squashfs")
		if err := copyFile(squashfsSrc, squashfsDst); err != nil {
			log.Printf("Warning: failed to copy filesystem.squashfs: %v", err)
		} else {
			log.Printf("Extracted: casper/filesystem.squashfs")
		}
	}

	// Copy syslinux PXE bootloader files from ISO.
	pxeFiles := []string{"pxelinux.0", "ldlinux.c32", "libutil.c32", "menu.c32"}
	isoSearchDirs := []string{"isolinux", "syslinux", "boot/syslinux"}

	for _, pxeFile := range pxeFiles {
		for _, searchDir := range isoSearchDirs {
			src := filepath.Join(mountPoint, searchDir, pxeFile)
			if fileExists(src) {
				dst := filepath.Join(outputDir, pxeFile)
				if err := copyFile(src, dst); err != nil {
					log.Printf("Warning: failed to copy %s: %v", pxeFile, err)
				} else {
					log.Printf("Extracted PXE loader: %s", pxeFile)
				}
				break
			}
		}
	}

	// If pxelinux.0 not found in ISO, try system syslinux paths.
	pxeBootloader := filepath.Join(outputDir, "pxelinux.0")
	if !fileExists(pxeBootloader) {
		systemPXEPaths := []string{
			"/usr/lib/PXELINUX/pxelinux.0",
			"/usr/share/syslinux/pxelinux.0",
			"/usr/lib/syslinux/modules/bios/pxelinux.0",
		}
		for _, sysPath := range systemPXEPaths {
			if fileExists(sysPath) {
				if err := copyFile(sysPath, pxeBootloader); err == nil {
					log.Printf("Copied system pxelinux.0 from %s", sysPath)
				}
				break
			}
		}

		// Also copy ldlinux.c32 from system.
		ldlinuxDst := filepath.Join(outputDir, "ldlinux.c32")
		if !fileExists(ldlinuxDst) {
			systemLDPaths := []string{
				"/usr/lib/syslinux/modules/bios/ldlinux.c32",
				"/usr/share/syslinux/ldlinux.c32",
			}
			for _, ldPath := range systemLDPaths {
				if fileExists(ldPath) {
					if err := copyFile(ldPath, ldlinuxDst); err == nil {
						log.Printf("Copied system ldlinux.c32 from %s", ldPath)
					}
					break
				}
			}
		}
	}

	return nil
}

// copyFileIfExists copies src to dst only if src exists.
func copyFileIfExists(src, dst string) error {
	if !fileExists(src) {
		return nil
	}
	return copyFile(src, dst)
}

// copyFile copies a single file from src to dst, preserving permissions.
func copyFile(src, dst string) error {
	srcFile, err := os.Open(src)
	if err != nil {
		return fmt.Errorf("open source %s: %w", src, err)
	}
	defer srcFile.Close()

	srcInfo, err := srcFile.Stat()
	if err != nil {
		return fmt.Errorf("stat source %s: %w", src, err)
	}

	dstFile, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, srcInfo.Mode())
	if err != nil {
		return fmt.Errorf("create destination %s: %w", dst, err)
	}
	defer dstFile.Close()

	if _, err := io.Copy(dstFile, srcFile); err != nil {
		return fmt.Errorf("copy %s -> %s: %w", src, dst, err)
	}

	return nil
}

// fileExists returns true if path exists and is not a directory.
func fileExists(path string) bool {
	info, err := os.Stat(path)
	if err != nil {
		return false
	}
	return !info.IsDir()
}
