# ============================================================
# HyveOS Build Targets
#
# Usage:
#   make iso                  Build server variant ISO
#   make iso VARIANT=desktop  Build desktop variant ISO
#   make iso-all              Build all variant ISOs
#   make iso-test             Build + launch in QEMU
#   make bundle               Build deployment tar.gz
#   make pxe-server           Start PXE server with latest ISO
#   make clean                Remove build artifacts
# ============================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Configuration
VARIANT   ?= server
VERSION   ?= 1.0.0
ARCH      ?= amd64
OUTPUT    ?= $(CURDIR)/../../dist
REPO_ROOT ?= $(CURDIR)/../..

ISO_NAME   = hyve-os-$(VERSION)-$(VARIANT)-$(ARCH)
ISO_PATH   = $(OUTPUT)/$(ISO_NAME).iso

# QEMU settings
QEMU_RAM  ?= 4096
QEMU_CPUS ?= 2
QEMU_DISK ?= 20G

# ─── Help ───────────────────────────────────────────────────

.PHONY: help
help:
	@echo ""
	@echo "  HyveOS Build System"
	@echo "  ==================="
	@echo ""
	@echo "  ISO Targets:"
	@echo "    make iso              Build ISO (VARIANT=$(VARIANT))"
	@echo "    make iso-all          Build all variants (server, desktop, edge)"
	@echo "    make iso-test         Build + launch in QEMU"
	@echo "    make iso-desktop      Shortcut: build desktop variant"
	@echo "    make iso-edge         Shortcut: build edge variant"
	@echo ""
	@echo "  Bundle Targets:"
	@echo "    make bundle           Build deployment tar.gz"
	@echo ""
	@echo "  Network Targets:"
	@echo "    make pxe-server       Start PXE boot server"
	@echo ""
	@echo "  Utility:"
	@echo "    make verify           Verify ISO checksum"
	@echo "    make clean            Remove build artifacts"
	@echo ""
	@echo "  Variables:"
	@echo "    VARIANT=$(VARIANT)  VERSION=$(VERSION)  ARCH=$(ARCH)"
	@echo "    QEMU_RAM=$(QEMU_RAM)  QEMU_CPUS=$(QEMU_CPUS)"
	@echo ""

# ─── ISO Build ──────────────────────────────────────────────

.PHONY: iso
iso:
	@echo "Building HyveOS ISO: $(ISO_NAME)"
	sudo bash $(CURDIR)/../build-iso.sh \
		--variant $(VARIANT) \
		--version $(VERSION) \
		--output $(OUTPUT)
	@echo ""
	@echo "ISO ready: $(ISO_PATH)"

.PHONY: iso-all
iso-all:
	@for v in server desktop edge; do \
		echo "=== Building $$v variant ==="; \
		sudo bash $(CURDIR)/../build-iso.sh \
			--variant $$v \
			--version $(VERSION) \
			--output $(OUTPUT); \
	done
	@echo ""
	@echo "All ISOs ready in $(OUTPUT)/"
	@ls -lh $(OUTPUT)/hyve-os-*.iso 2>/dev/null

.PHONY: iso-desktop
iso-desktop:
	$(MAKE) iso VARIANT=desktop

.PHONY: iso-edge
iso-edge:
	$(MAKE) iso VARIANT=edge

# ─── QEMU Testing ──────────────────────────────────────────

.PHONY: iso-test
iso-test: iso
	@echo "Launching HyveOS in QEMU..."
	@echo "  RAM: $(QEMU_RAM)MB  CPUs: $(QEMU_CPUS)"
	@echo "  Press Ctrl+A X to exit QEMU"
	qemu-system-x86_64 \
		-m $(QEMU_RAM) \
		-smp $(QEMU_CPUS) \
		-cdrom $(ISO_PATH) \
		-boot d \
		-enable-kvm \
		-net nic -net user,hostfwd=tcp::16777-:6777 \
		-drive file=$(OUTPUT)/hyve-test-disk.qcow2,format=qcow2,if=virtio,size=$(QEMU_DISK)

.PHONY: iso-test-nokvm
iso-test-nokvm: iso
	@echo "Launching HyveOS in QEMU (no KVM)..."
	qemu-system-x86_64 \
		-m $(QEMU_RAM) \
		-smp $(QEMU_CPUS) \
		-cdrom $(ISO_PATH) \
		-boot d \
		-net nic -net user,hostfwd=tcp::16777-:6777

# ─── Bundle ─────────────────────────────────────────────────

.PHONY: bundle
bundle:
	@echo "Building deployment bundle..."
	bash $(REPO_ROOT)/deploy/linux/build_bundle.sh
	@echo "Bundle ready in $(OUTPUT)/"
	@ls -lh $(OUTPUT)/hyve-os-*.tar.gz 2>/dev/null

# ─── PXE Server ─────────────────────────────────────────────

.PHONY: pxe-server
pxe-server:
	@if [ ! -f "$(ISO_PATH)" ]; then \
		echo "ISO not found: $(ISO_PATH)"; \
		echo "Run 'make iso' first."; \
		exit 1; \
	fi
	@echo "Starting PXE server with $(ISO_PATH)..."
	@echo "Configure DHCP to point to this machine for PXE boot."
	sudo python3 $(CURDIR)/../pxe/hyve-pxe-server.py \
		--iso $(ISO_PATH) \
		--interface eth0

# ─── Verification ───────────────────────────────────────────

.PHONY: verify
verify:
	@if [ ! -f "$(ISO_PATH).sha256" ]; then \
		echo "Checksum file not found: $(ISO_PATH).sha256"; \
		exit 1; \
	fi
	cd $(OUTPUT) && sha256sum -c $(ISO_NAME).iso.sha256
	@echo "Checksum verified OK"

# ─── Cleanup ────────────────────────────────────────────────

.PHONY: clean
clean:
	rm -f $(OUTPUT)/hyve-os-*.iso
	rm -f $(OUTPUT)/hyve-os-*.iso.sha256
	rm -f $(OUTPUT)/hyve-os-*.tar.gz
	rm -f $(OUTPUT)/hyve-test-disk.qcow2
	rm -rf /tmp/hyve-iso-build-*
	@echo "Build artifacts cleaned"
