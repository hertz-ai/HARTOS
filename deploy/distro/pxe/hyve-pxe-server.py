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
import ssl
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
        # Security: prevent path traversal (null bytes, .., backslash tricks)
        filename = os.path.normpath(filename.replace('\x00', '')).lstrip('/').lstrip('\\')
        # Remove any remaining .. components
        filename = '/'.join(p for p in filename.split('/') if p != '..')
        filepath = os.path.join(self.server.serve_dir, filename)
        # Fail-closed: verify path stays under serve_dir
        try:
            common = os.path.commonpath([os.path.abspath(filepath), os.path.abspath(self.server.serve_dir)])
            if common != os.path.abspath(self.server.serve_dir):
                logger.warning("[TFTP] Path traversal blocked: %s", filename)
                self._send_error(sock, 2, "Access denied")
                return
        except ValueError:
            self._send_error(sock, 2, "Access denied")
            return

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


def _generate_self_signed_cert(cert_path: str, key_path: str, hostname: str = None):
    """Generate a self-signed TLS certificate for PXE HTTPS serving.

    Uses subprocess openssl to create a temporary self-signed cert.
    Falls back to Python's ssl module if openssl CLI is unavailable.

    Args:
        cert_path: Path to write the certificate PEM file.
        key_path: Path to write the private key PEM file.
        hostname: Common Name for the certificate (defaults to machine hostname).
    """
    if hostname is None:
        hostname = socket.gethostname()

    subject = f"/CN={hostname}/O=HyveOS PXE Server"

    try:
        # Prefer openssl CLI for broader compatibility
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-keyout', key_path, '-out', cert_path,
            '-days', '365', '-nodes',
            '-subj', subject,
        ], check=True, capture_output=True)
        logger.info("Generated self-signed TLS certificate: %s (CN=%s)", cert_path, hostname)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        # Fallback: use Python's ssl module to generate via openssl wrapper
        # This requires the cryptography library
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import datetime

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

            name = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, hostname),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HyveOS PXE Server"),
            ])

            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.utcnow())
                .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
                .add_extension(
                    x509.SubjectAlternativeName([
                        x509.DNSName(hostname),
                        x509.DNSName("localhost"),
                    ]),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )

            with open(key_path, 'wb') as f:
                f.write(key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                ))

            with open(cert_path, 'wb') as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))

            logger.info("Generated self-signed TLS certificate (Python cryptography): %s", cert_path)
        except ImportError:
            logger.error(
                "Cannot generate self-signed cert: neither openssl CLI nor "
                "Python cryptography library available. Install one of them or "
                "provide --tls-cert and --tls-key manually."
            )
            raise RuntimeError("No TLS certificate generation method available") from e


def start_http_server(serve_dir: str, port: int, tls_cert: str = None, tls_key: str = None):
    """Start HTTP server for autoinstall + squashfs.

    Args:
        serve_dir: Directory to serve files from.
        port: TCP port to listen on.
        tls_cert: Path to TLS certificate PEM file (optional).
        tls_key: Path to TLS private key PEM file (optional).
    """
    handler = lambda *args, **kwargs: PXEHTTPHandler(
        *args, serve_dir=serve_dir, **kwargs)

    with socketserver.TCPServer(("", port), handler) as httpd:
        if tls_cert and tls_key:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
            # Secure defaults
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)
            logger.info("HTTPS server listening on port %d (TLS enabled, dir: %s)",
                        port, serve_dir)
        else:
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
    parser.add_argument('--tls-cert', default=None,
                        help='Path to TLS certificate PEM file for HTTPS')
    parser.add_argument('--tls-key', default=None,
                        help='Path to TLS private key PEM file for HTTPS')
    parser.add_argument('--tls-auto', action='store_true', default=False,
                        help='Auto-generate a self-signed cert if --tls-cert/--tls-key not provided')
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

    # ─── TLS Setup (optional) ───
    tls_cert = args.tls_cert
    tls_key = args.tls_key

    if args.tls_auto and not (tls_cert and tls_key):
        # Auto-generate a self-signed certificate
        tls_dir = os.path.join(serve_dir, '.tls')
        os.makedirs(tls_dir, exist_ok=True)
        tls_cert = os.path.join(tls_dir, 'pxe-server.crt')
        tls_key = os.path.join(tls_dir, 'pxe-server.key')
        if not (os.path.exists(tls_cert) and os.path.exists(tls_key)):
            _generate_self_signed_cert(tls_cert, tls_key, hostname=server_ip)

    if tls_cert and tls_key:
        if not os.path.exists(tls_cert):
            logger.error("TLS certificate not found: %s", tls_cert)
            sys.exit(1)
        if not os.path.exists(tls_key):
            logger.error("TLS key not found: %s", tls_key)
            sys.exit(1)

    # Start TFTP server in background thread
    # Note: TFTP is UDP-based and does not support TLS (this is standard)
    proto = "https" if (tls_cert and tls_key) else "http"
    logger.info("Starting HyveOS PXE server...")
    logger.info("  TFTP: 0.0.0.0:%d (UDP, no TLS)", args.tftp_port)
    logger.info("  %s: %s://0.0.0.0:%d", proto.upper(), proto, args.port)
    logger.info("  Autoinstall: %s://%s:%d/autoinstall/", proto, server_ip, args.port)

    tftp_thread = threading.Thread(
        target=start_tftp_server,
        args=(serve_dir, args.tftp_port),
        daemon=True,
    )
    tftp_thread.start()

    # Start HTTP/HTTPS server (main thread — blocks)
    start_http_server(serve_dir, args.port, tls_cert=tls_cert, tls_key=tls_key)


if __name__ == '__main__':
    main()
