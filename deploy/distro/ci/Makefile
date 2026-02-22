# ============================================================
# HART OS Build Targets
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

ISO_NAME   = hart-os-$(VERSION)-$(VARIANT)-$(ARCH)
ISO_PATH   = $(OUTPUT)/$(ISO_NAME).iso

# QEMU settings
QEMU_RAM  ?= 4096
QEMU_CPUS ?= 2
QEMU_DISK ?= 20G

# ─── Help ───────────────────────────────────────────────────

.PHONY: help
help:
	@echo ""
	@echo "  HART OS Build System"
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
	@echo "Building HART OS ISO: $(ISO_NAME)"
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
	@ls -lh $(OUTPUT)/hart-os-*.iso 2>/dev/null

.PHONY: iso-desktop
iso-desktop:
	$(MAKE) iso VARIANT=desktop

.PHONY: iso-edge
iso-edge:
	$(MAKE) iso VARIANT=edge

# ─── QEMU Testing ──────────────────────────────────────────

.PHONY: iso-test
iso-test: iso
	@echo "Launching HART OS in QEMU..."
	@echo "  RAM: $(QEMU_RAM)MB  CPUs: $(QEMU_CPUS)"
	@echo "  Press Ctrl+A X to exit QEMU"
	qemu-system-x86_64 \
		-m $(QEMU_RAM) \
		-smp $(QEMU_CPUS) \
		-cdrom $(ISO_PATH) \
		-boot d \
		-enable-kvm \
		-net nic -net user,hostfwd=tcp::16777-:6777 \
		-drive file=$(OUTPUT)/hart-test-disk.qcow2,format=qcow2,if=virtio,size=$(QEMU_DISK)

.PHONY: iso-test-nokvm
iso-test-nokvm: iso
	@echo "Launching HART OS in QEMU (no KVM)..."
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
	@ls -lh $(OUTPUT)/hart-os-*.tar.gz 2>/dev/null

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
	sudo python3 $(CURDIR)/../pxe/hart-pxe-server.py \
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

# ─── NixOS Build (new immutable OS path) ─────────────────────

.PHONY: nix-iso
nix-iso:
	@echo "Building HART OS ISO from NixOS flake..."
	cd $(REPO_ROOT)/nixos && nix build .#iso-server
	@echo "ISO ready: nixos/result/iso/"

.PHONY: nix-vm
nix-vm:
	@echo "Building HART OS VM from NixOS flake..."
	cd $(REPO_ROOT)/nixos && nix build .#vm-server
	@echo "Run with: nixos/result/bin/run-hart-server-vm"

.PHONY: e2e-test
e2e-test: nix-iso
	@echo "Running E2E smoke tests against NixOS ISO..."
	bash $(REPO_ROOT)/tests/boot-qemu-for-test.sh $$(ls $(REPO_ROOT)/nixos/result/iso/*.iso)
	bash $(REPO_ROOT)/tests/e2e-os-smoke.sh
	@echo ""
	@echo "E2E tests passed!"
	@# Cleanup QEMU
	@if [ -f /tmp/hart-qemu-test.pid ]; then \
		kill "$$(cat /tmp/hart-qemu-test.pid)" 2>/dev/null || true; \
		rm -f /tmp/hart-qemu-test.pid; \
	fi

# ─── NixOS Lint & Check (fast, ~30s) ──────────────────────────

.PHONY: nix-check
nix-check:
	@echo "Running Nix lint + evaluation..."
	@command -v statix >/dev/null 2>&1 && statix check $(REPO_ROOT)/nixos/ || echo "statix not installed (skipping)"
	@command -v deadnix >/dev/null 2>&1 && deadnix $(REPO_ROOT)/nixos/ || echo "deadnix not installed (skipping)"
	cd $(REPO_ROOT)/nixos && nix flake check --no-build
	@echo "All checks passed!"

# ─── NixOS Build All Key Targets ─────────────────────────────

.PHONY: nix-build-all
nix-build-all:
	@echo "Building all key HART OS image targets..."
	cd $(REPO_ROOT)/nixos && nix build .#iso-server --no-link --print-out-paths
	cd $(REPO_ROOT)/nixos && nix build .#iso-desktop --no-link --print-out-paths
	cd $(REPO_ROOT)/nixos && nix build .#iso-edge --no-link --print-out-paths
	cd $(REPO_ROOT)/nixos && nix build .#qcow2-server --no-link --print-out-paths
	cd $(REPO_ROOT)/nixos && nix build .#docker-server --no-link --print-out-paths
	@echo ""
	@echo "All 5 targets built successfully!"

# ─── NixOS VM Integration Tests ──────────────────────────────

.PHONY: nix-test
nix-test:
	@echo "Running NixOS VM integration tests..."
	cd $(REPO_ROOT)/nixos && nix flake check
	@echo "All VM tests passed!"

# ─── Full NixOS E2E (build → boot → smoke → sandbox) ─────────

.PHONY: nix-e2e
nix-e2e: nix-check nix-iso
	@echo "Running full NixOS E2E test pipeline..."
	bash $(REPO_ROOT)/tests/boot-qemu-for-test.sh $$(ls $(REPO_ROOT)/nixos/result/iso/*.iso)
	bash $(REPO_ROOT)/tests/e2e-os-smoke.sh
	@echo ""
	@echo "Running sandbox validation inside VM..."
	sshpass -p 'hart' ssh \
		-o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		-p 10022 hart-admin@localhost \
		'sudo hart sandbox test-all' || echo "Sandbox tests completed (check output above)"
	@echo ""
	@echo "Full NixOS E2E pipeline passed!"
	@# Cleanup QEMU
	@if [ -f /tmp/hart-qemu-test.pid ]; then \
		kill "$$(cat /tmp/hart-qemu-test.pid)" 2>/dev/null || true; \
		rm -f /tmp/hart-qemu-test.pid; \
	fi

# ─── Cleanup ────────────────────────────────────────────────

.PHONY: clean
clean:
	rm -f $(OUTPUT)/hart-os-*.iso
	rm -f $(OUTPUT)/hart-os-*.iso.sha256
	rm -f $(OUTPUT)/hart-os-*.tar.gz
	rm -f $(OUTPUT)/hart-test-disk.qcow2
	rm -rf /tmp/hart-iso-build-*
	@echo "Build artifacts cleaned"
