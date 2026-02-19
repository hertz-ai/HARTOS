#!/usr/bin/env python3
"""
HyveOS PXE Boot Server - TFTP + HTTP for network booting.

Serves the HyveOS kernel, initrd, and autoinstall configs so target
machines can boot and install HyveOS over the network.

Usage:
    sudo python hyve-pxe-server.py --iso /path/to/hyve-os.iso [--port 8888] [--interface eth0]

Requires:
    - An existing DHCP server configured with:
      option 66 (next-server): this machine's IP
      option 67 (filename): pxelinux.0
    - OR: dnsmasq in proxy mode (instructions printed on start)

Flow:
    1. Target machine PXE boots
    2. DHCP directs to this server
    3. TFTP serves pxelinux.0 + config + kernel + initrd
    4. HTTP serves autoinstall configs + squashfs
    5. Ubuntu installer runs with HyveOS autoinstall
    6. After install, first-boot setup runs
"""

import argparse
import http.server
import logging
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading

logger = logging.getLogger('hyve-pxe')

DEFAULT_HTTP_PORT = 8888
DEFAULT_TFTP_PORT = 69


# ─── TFTP Server Implementation ───

class TFTPHandler(socketserver.BaseRequestHandler):
    """Minimal TFTP server handler (RFC 1350).

    Supports only read requests (RRQ) — sufficient for PXE boot.
    """

    BLOCK_SIZE = 512

    def handle(self):
        data, sock = self.request
        if len(data) < 4:
            return

        opcode = struct.unpack('!H', data[:2])[0]

        if opcode == 1:  # RRQ (Read Request)
            self._handle_rrq(data[2:], sock)
        else:
            self._send_error(sock, 4, "Illegal operation")

    def _handle_rrq(self, data, sock):
        """Handle a TFTP read request."""
        parts = data.split(b'\x00')
        if len(parts) < 2:
            return

        filename = parts[0].decode('ascii', errors='replace')
        # Security: prevent path traversal
        filename = filename.replace('..', '').lstrip('/')

        filepath = os.path.join(self.server.serve_dir, filename)

        if not os.path.isfile(filepath):
            logger.warning("[TFTP] File not found: %s", filename)
            self._send_error(sock, 1, "File not found")
            return

        logger.info("[TFTP] Serving: %s (%d bytes)",
                     filename, os.path.getsize(filepath))

        try:
            with open(filepath, 'rb') as f:
                block_num = 1
                while True:
                    chunk = f.read(self.BLOCK_SIZE)
                    # DATA packet: opcode=3, block#, data
                    packet = struct.pack('!HH', 3, block_num) + chunk
                    sock.sendto(packet, self.client_address)

                    # Wait for ACK
                    sock.settimeout(5)
                    try:
                        ack_data, _ = sock.recvfrom(4)
                        ack_opcode = struct.unpack('!H', ack_data[:2])[0]
                        ack_block = struct.unpack('!H', ack_data[2:4])[0]
                        if ack_opcode != 4 or ack_block != block_num:
                            break
                    except socket.timeout:
                        logger.warning("[TFTP] Timeout waiting for ACK block %d", block_num)
                        break

                    if len(chunk) < self.BLOCK_SIZE:
                        break  # Last block
                    block_num += 1

        except Exception as e:
            logger.error("[TFTP] Error serving %s: %s", filename, e)
            self._send_error(sock, 0, str(e))

    def _send_error(self, sock, code, msg):
        """Send TFTP error packet."""
        packet = struct.pack('!HH', 5, code) + msg.encode() + b'\x00'
        sock.sendto(packet, self.client_address)


class TFTPServer(socketserver.UDPServer):
    """TFTP server with configurable serve directory."""

    allow_reuse_address = True

    def __init__(self, server_address, handler_class, serve_dir):
        self.serve_dir = serve_dir
        super().__init__(server_address, handler_class)


# ─── HTTP Server ───

class PXEHTTPHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler that serves autoinstall configs and ISO contents."""

    def __init__(self, *args, serve_dir=None, **kwargs):
        self.serve_dir = serve_dir or '.'
        super().__init__(*args, directory=self.serve_dir, **kwargs)

    def log_message(self, format, *args):
        logger.info("[HTTP] %s", format % args)


# ─── ISO Extraction ───

def extract_iso(iso_path: str, output_dir: str):
    """Extract boot files from ISO for PXE serving."""
    logger.info("Extracting ISO: %s -> %s", iso_path, output_dir)

    mount_point = tempfile.mkdtemp(prefix='hyve-iso-')

    try:
        subprocess.run(
            ['mount', '-o', 'loop,ro', iso_path, mount_point],
            check=True, capture_output=True,
        )

        # Copy kernel and initrd
        for src, dst in [
            ('casper/vmlinuz', 'vmlinuz'),
            ('casper/initrd', 'initrd'),
        ]:
            src_path = os.path.join(mount_point, src)
            dst_path = os.path.join(output_dir, dst)
            if os.path.exists(src_path):
                shutil.copy2(src_path, dst_path)
                logger.info("Extracted: %s", dst)

        # Copy squashfs for HTTP serving
        squashfs_src = os.path.join(mount_point, 'casper/filesystem.squashfs')
        if os.path.exists(squashfs_src):
            os.makedirs(os.path.join(output_dir, 'casper'), exist_ok=True)
            shutil.copy2(squashfs_src,
                         os.path.join(output_dir, 'casper/filesystem.squashfs'))
            logger.info("Extracted: casper/filesystem.squashfs")

        # Copy syslinux PXE bootloader files
        for pxe_file in ['pxelinux.0', 'ldlinux.c32', 'libutil.c32', 'menu.c32']:
            for search_dir in ['isolinux', 'syslinux', 'boot/syslinux']:
                src = os.path.join(mount_point, search_dir, pxe_file)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(output_dir, pxe_file))
                    logger.info("Extracted PXE loader: %s", pxe_file)
                    break

        # If pxelinux.0 not in ISO, try system syslinux
        if not os.path.exists(os.path.join(output_dir, 'pxelinux.0')):
            for system_path in [
                '/usr/lib/PXELINUX/pxelinux.0',
                '/usr/share/syslinux/pxelinux.0',
                '/usr/lib/syslinux/modules/bios/pxelinux.0',
            ]:
                if os.path.exists(system_path):
                    shutil.copy2(system_path, os.path.join(output_dir, 'pxelinux.0'))
                    logger.info("Copied system pxelinux.0 from %s", system_path)
                    break
            # Also copy ldlinux.c32
            for ld_path in [
                '/usr/lib/syslinux/modules/bios/ldlinux.c32',
                '/usr/share/syslinux/ldlinux.c32',
            ]:
                if os.path.exists(ld_path):
                    shutil.copy2(ld_path, os.path.join(output_dir, 'ldlinux.c32'))
                    break

    finally:
        subprocess.run(['umount', mount_point], check=False)
        os.rmdir(mount_point)


def get_server_ip(interface: str = None) -> str:
    """Get the IP address of this machine."""
    if interface:
        try:
            import fcntl
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ip = socket.inet_ntoa(fcntl.ioctl(
                s.fileno(), 0x8915,  # SIOCGIFADDR
                struct.pack('256s', interface.encode()[:15])
            )[20:24])
            return ip
        except Exception:
            pass

    # Fallback: connect to external and read local address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


def setup_pxe_config(output_dir: str, server_ip: str, http_port: int):
    """Write PXE boot config with correct server IP substituted."""
    pxe_cfg_dir = os.path.join(output_dir, 'pxelinux.cfg')
    os.makedirs(pxe_cfg_dir, exist_ok=True)

    config = f"""DEFAULT hyve-auto
PROMPT 1
TIMEOUT 100
MENU TITLE HyveOS Network Install

LABEL hyve-auto
    MENU LABEL ^HyveOS — Automatic Install
    MENU DEFAULT
    KERNEL vmlinuz
    APPEND initrd=initrd ip=dhcp autoinstall ds=nocloud-net;s=http://{server_ip}:{http_port}/autoinstall/ ---

LABEL hyve-manual
    MENU LABEL HyveOS — ^Manual Install
    KERNEL vmlinuz
    APPEND initrd=initrd ip=dhcp ---

LABEL local
    MENU LABEL Boot from ^local disk
    LOCALBOOT 0
"""
    with open(os.path.join(pxe_cfg_dir, 'default'), 'w') as f:
        f.write(config)
    logger.info("PXE config written with server IP: %s", server_ip)


def setup_autoinstall_dir(output_dir: str):
    """Copy autoinstall configs to serve directory."""
    autoinstall_dir = os.path.join(output_dir, 'autoinstall')
    os.makedirs(autoinstall_dir, exist_ok=True)

    repo_autoinstall = os.path.join(
        os.path.dirname(__file__), '..', 'autoinstall')

    for filename in ['user-data', 'meta-data', 'vendor-data']:
        src = os.path.join(repo_autoinstall, filename)
        dst = os.path.join(autoinstall_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, dst)


def start_http_server(serve_dir: str, port: int):
    """Start HTTP server for autoinstall + squashfs."""
    handler = lambda *args, **kwargs: PXEHTTPHandler(
        *args, serve_dir=serve_dir, **kwargs)

    with socketserver.TCPServer(("", port), handler) as httpd:
        logger.info("HTTP server listening on port %d (dir: %s)", port, serve_dir)
        httpd.serve_forever()


def start_tftp_server(serve_dir: str, port: int):
    """Start TFTP server for PXE boot files."""
    server = TFTPServer(("", port), TFTPHandler, serve_dir)
    logger.info("TFTP server listening on port %d (dir: %s)", port, serve_dir)
    server.serve_forever()


def print_dnsmasq_hint(server_ip: str, serve_dir: str):
    """Print dnsmasq proxy DHCP configuration hint."""
    logger.info(
        "\n"
        "  ┌─────────────────────────────────────────────────────────┐\n"
        "  │ DHCP Configuration Required                            │\n"
        "  │                                                        │\n"
        "  │ Option A: Configure existing DHCP server:              │\n"
        "  │   next-server: %s                        │\n"
        "  │   filename: pxelinux.0                                 │\n"
        "  │                                                        │\n"
        "  │ Option B: Use dnsmasq proxy mode:                      │\n"
        "  │   apt install dnsmasq                                  │\n"
        "  │   # /etc/dnsmasq.d/hyve-pxe.conf:                     │\n"
        "  │   port=0                                               │\n"
        "  │   dhcp-range=<subnet>,proxy                            │\n"
        "  │   pxe-service=x86PC,\"HyveOS\",pxelinux                 │\n"
        "  │   enable-tftp                                          │\n"
        "  │   tftp-root=%s                             │\n"
        "  └─────────────────────────────────────────────────────────┘\n",
        server_ip.ljust(24), serve_dir[:30].ljust(24),
    )


def main():
    parser = argparse.ArgumentParser(
        description='HyveOS PXE Boot Server',
    )
    parser.add_argument('--iso', help='Path to HyveOS ISO file')
    parser.add_argument('--port', type=int, default=DEFAULT_HTTP_PORT,
                        help=f'HTTP port (default: {DEFAULT_HTTP_PORT})')
    parser.add_argument('--tftp-port', type=int, default=DEFAULT_TFTP_PORT,
                        help=f'TFTP port (default: {DEFAULT_TFTP_PORT})')
    parser.add_argument('--interface', default=None,
                        help='Network interface to bind to (e.g., eth0)')
    parser.add_argument('--serve-dir', default='/srv/hyve-pxe',
                        help='Directory to serve PXE files from')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(message)s',
    )

    serve_dir = args.serve_dir
    os.makedirs(serve_dir, exist_ok=True)

    # Determine server IP
    server_ip = get_server_ip(args.interface)
    logger.info("Server IP: %s", server_ip)

    # Extract ISO if provided
    if args.iso:
        if not os.path.exists(args.iso):
            logger.error("ISO not found: %s", args.iso)
            sys.exit(1)
        extract_iso(args.iso, serve_dir)

    # Setup PXE config with actual server IP
    setup_pxe_config(serve_dir, server_ip, args.port)

    # Setup autoinstall configs
    setup_autoinstall_dir(serve_dir)

    # Print DHCP configuration hint
    print_dnsmasq_hint(server_ip, serve_dir)

    # Verify pxelinux.0 exists
    if not os.path.exists(os.path.join(serve_dir, 'pxelinux.0')):
        logger.warning("pxelinux.0 not found! Install: apt install pxelinux syslinux-common")

    # Start TFTP server in background thread
    logger.info("Starting HyveOS PXE server...")
    logger.info("  TFTP: 0.0.0.0:%d", args.tftp_port)
    logger.info("  HTTP: http://0.0.0.0:%d", args.port)
    logger.info("  Autoinstall: http://%s:%d/autoinstall/", server_ip, args.port)

    tftp_thread = threading.Thread(
        target=start_tftp_server,
        args=(serve_dir, args.tftp_port),
        daemon=True,
    )
    tftp_thread.start()

    # Start HTTP server (main thread — blocks)
    start_http_server(serve_dir, args.port)


if __name__ == '__main__':
    main()
